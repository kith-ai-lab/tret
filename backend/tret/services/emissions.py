"""Emissions accounting: energy, data-centre overhead, GHG Protocol scopes.

This module owns every carbon number tret prints. It grew out of
`providers/catalog.py`'s energy accounting, which still re-exports the names it
used to own so existing imports keep working.

What is here, and what each part is worth:

* **Energy classes** (`ENERGY_CLASS_WH_PER_MTOK`) — Wh per million
  *output-equivalent* tokens. The shipped (v2) ladder is a single-coefficient
  nonnegative fit (output + a fixed 0.05x input weight) against
  source-PUE-normalised Jegham et al. 2025 (arXiv:2505.09598) observations;
  `class_ladder_v1`, the original two-parameter least-squares fit on the
  as-published (source-PUE-inclusive) figures, is retained only for rollback.
  Still an estimate; no longer a hand-picked one. `ENERGY_CLASS_CALIBRATION`
  carries the fit each class was anchored on so a reader can reproduce it.
* **Token weighting** — input and output tokens are *not* equally expensive.
  Prefill is parallel, generation is sequential. `ENERGY_TOKEN_WEIGHTS` holds
  the ratios; input is weighted 0.05 of output by fixed assumption (v2) — the
  v1 fit's ~20x ratio is the origin of that number. A cache read is a tenth of
  an input token, a cache write is a full prefill pass.
* **PUE** — data-centre overhead, resolved per deployment profile
  (hyperscaler cloud / workstation / on-prem facility). `energy_wh` stays the
  *compute* (IT-load) figure it has always been; `energy_wh_total` is
  compute x PUE.
* **Grid intensity** — Ember's World 2025 lifecycle CO2e intensity by default
  (458.49 gCO2e/kWh), optionally
  replaced **per provider** by operator configuration (`TRET_GRID_FACTORS`),
  carrying an explicit GHG Protocol **basis** label (location-based /
  market-based / unspecified) because mixing the two is meaningless, and a
  stable `grid_co2e_source` key saying *which* rule chose the factor. It is
  configuration, never inference: tret does not geolocate anything and makes
  no network call to resolve a factor (`GRID_NO_INFERENCE_NOTE`).
* **Scopes** — the GHG Protocol mapping for the *tret operator*: Scope 1 is
  always 0, self-hosted electricity is Scope 2, cloud inference is Scope 3
  (purchased service), and amortized local hardware is Scope 3 (capital goods).
  `_SCOPE_BASIS` states the reasoning inline, on every run.
* **Uncertainty** — an explicit multiplicative *judgment band* (2.5x either way
  by default), never a confidence interval, because no credible methodology in
  this field publishes one. `contributions` decomposes it per factor so a
  sensitivity view is possible.
* **Frontier baseline** — a same-token counterfactual in both carbon and
  dollars: what the identical token counts would have emitted, and cost, on the
  heaviest curated cloud model. An efficiency indicator, not an offset, not a
  reduction claim, and not usable for statutory reporting. `avoided_co2e_g` and
  `avoided_usd` are signed: a run heavier or dearer than the baseline reports a
  negative figure rather than a clamped zero.
* **Provenance** — every constant above is also emitted as a structured factor
  (`factors`) carrying value, unit, source, url, date and a confidence marker,
  so "where did this number come from" is answerable from the stored run alone,
  with nothing hardcoded in the frontend.

Read docs/emissions-methodology.md before quoting any of these numbers. The
honest use of the output is comparison between two model choices — never
disclosure.

A note on size: the block is self-describing, which costs roughly 15 KB of JSONB
per run (against a `messages` transcript that is usually far larger, and JSONB
TOAST-compresses). That is the deliberate price of the provenance requirement —
a stored run has to explain itself without a lookup table living in the
frontend. If it ever becomes a problem, the fix is for the `/emissions` rollup to
stop selecting the whole column, not for the factors to stop citing their
sources.

The catalog is imported lazily (inside functions) because `providers/catalog.py`
imports this module at import time; keep it that way.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tret.config import (
    GRID_BASES,
    GRID_BASIS_LOCATION,
    GRID_BASIS_MARKET,
    GRID_BASIS_UNSPECIFIED,
    GridFactor,
    Settings,
    get_settings,
)
from tret.services.uncertainty_derivation import (
    Evidence,
    adjust_contributions,
    band_record,
    derive_band,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tret.providers.base import Usage
    from tret.providers.catalog import ModelCatalog, ModelInfo
    from tret.services.emission_factors import FactorSet

# ── the calibration dataset ──────────────────────────────────────────────────
# Jegham, Abdelatti, Elmoubarki & Hendawi, "How Hungry is AI? Benchmarking
# Energy, Water, and Carbon Footprint of LLM Inference", arXiv:2505.09598,
# 14 May 2025. Wh per query at three prompt shapes — short 100in/300out, medium
# 1000in/1000out, long 10000in/1500out. The authors measured API latency and
# throughput and *inferred* the GPU class; it is the most granular per-model
# public data that exists, and it is still indirect.
#
# Fitting Wh = a x input + b x output by least squares over the three points per
# model (no intercept) gives the table below. tret's class constants are the
# fitted b values, and the input weight is the fitted b/a ratio. The working is
# reproduced in docs/emissions-methodology.md; `ENERGY_CLASS_CALIBRATION` below
# is the machine-readable version of the same table.
def _load_jegham_2025() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "data" / "calibration" / "jegham_2025_v1.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    observations = manifest["observations"]
    return {
        "citation": (
            "Jegham, Abdelatti, Elmoubarki & Hendawi, How Hungry is AI? Benchmarking "
            "Energy, Water, and Carbon Footprint of LLM Inference (arXiv:2505.09598)"
        ),
        # Preserve the historical public key while exposing the pinned v1 URL
        # separately for new provenance consumers.
        "url": "https://arxiv.org/abs/2505.09598",
        "source_version_url": manifest["source"]["url"],
        "date": manifest["source"]["published"],
        "shapes": tuple(tuple(shape) for shape in manifest["shapes"]),
        "wh_per_query": {
            model: tuple(row["legacy_rounded_wh"]) for model, row in observations.items()
        },
        "fit_wh_per_mtok": {
            model: tuple(values)
            for model, values in manifest["legacy_fit_wh_per_mtok"].items()
        },
        "manifest": manifest,
    }


JEGHAM_2025 = _load_jegham_2025()
"""Backward-compatible calibration view, loaded from the pinned v1 manifest."""

_DEGENERATE_FIT_NOTE = (
    "Two of the five fits are degenerate: GPT-4o (a = -6.1) and DeepSeek-R1 "
    "(a = -1,990) solve to a *negative* input coefficient, which is physically "
    "impossible — reading a token cannot generate energy. Three points and two "
    "free parameters leave no room to absorb reporting noise or a shifting batch "
    "size, and DeepSeek-R1's short-prompt figure (23.82 Wh, only 5.2 Wh below its "
    "medium figure) dominates the residual. For those models the fitted b is kept "
    "and a is discarded; the input weight comes from the two well-behaved fits "
    "instead, and is applied as a documented assumption rather than a measurement."
)

# ── energy classes ───────────────────────────────────────────────────────────
# Wh per million **output-equivalent** tokens: the unit of the calibration is a
# generated token, and every other bucket is expressed as a fraction of one (see
# ENERGY_TOKEN_WEIGHTS). Each class below is anchored on a fitted b from the
# table above, except XL, which is interpolated and says so.
#
# These remain estimates. They are calibrated against measured latency on
# inferred hardware for five models, then generalised to a catalog of models
# whose active parameter counts nobody publishes. The band in
# `uncertainty_band` is the honest expression of that.
ENERGY_CLASS_WH_PER_MTOK_V1: dict[str, Decimal] = {
    # GPT-4.1 nano fit: b = 271.9 Wh/Mtok. Small / distilled / nano-class served
    # models, and tret's default for local weights.
    "S": Decimal("250"),
    # GPT-4o fit: b = 1233.1 Wh/Mtok. Mid-size served models.
    "M": Decimal("1200"),
    # Claude 3.7 Sonnet fit: b = 2634.7 Wh/Mtok. Large frontier models.
    "L": Decimal("2600"),
    # No measured anchor. One geometric step above L (x2.3, the same step as
    # L/M) and well below the reasoning tier: the largest non-reasoning
    # flagships are believed heavier than Sonnet-class, but no public dataset
    # covers them. This is the one class constant that is still interpolation.
    "XL": Decimal("6000"),
    # o3 fit: b = 20850.4 Wh/Mtok. DeepSeek-R1's (degenerate) fit implies
    # 35,475 — so 21,000 sits at the *low* end of the two reasoning models in
    # the dataset, which is the unsafe direction and is called out in the docs.
    "R": Decimal("21000"),
}
ENERGY_CLASS_WH_PER_MTOK_V2: dict[str, Decimal] = {
    "S": Decimal("210.3398"),
    "M": Decimal("855.6651"),
    "L": Decimal("2399.3371"),
    # Explicit geometric continuation of the two adjacent fitted classes: L²/M.
    "XL": Decimal("6727.8875"),
    "R": Decimal("17713.3258"),
}
# The corrected node-IT ladder is the shipped default. V1 remains addressable
# for rollback and historical replay; stored rows are never recomputed.
ENERGY_CLASS_WH_PER_MTOK = ENERGY_CLASS_WH_PER_MTOK_V2
ENERGY_CLASSES = tuple(ENERGY_CLASS_WH_PER_MTOK)
DEFAULT_ENERGY_CLASS = "M"
# Models that spend hidden or visible "thinking" tokens before answering. Kept a
# separate class rather than a price tier: the dataset's reasoning models are
# 8-30x heavier than a large non-reasoning model, and one of them (DeepSeek-R1)
# is also one of the cheapest models on the market. Price does not predict this.
REASONING_ENERGY_CLASS = "R"

# Which fitted b each class was anchored on, so the ladder is auditable from the
# code rather than only from the doc.
ENERGY_CLASS_CALIBRATION: dict[str, dict[str, Any]] = {
    "S": {"anchor_model": "GPT-4.1 nano", "fitted_wh_per_mtok": 271.9, "measured": True},
    "M": {"anchor_model": "GPT-4o", "fitted_wh_per_mtok": 1233.1, "measured": True},
    "L": {"anchor_model": "Claude 3.7 Sonnet", "fitted_wh_per_mtok": 2634.7, "measured": True},
    "XL": {"anchor_model": None, "fitted_wh_per_mtok": None, "measured": False},
    "R": {"anchor_model": "o3", "fitted_wh_per_mtok": 20850.4, "measured": True},
}

# ── token weighting ──────────────────────────────────────────────────────────
# Weights are per *output-equivalent* token, so an output token is 1.0 by
# definition and everything else is a fraction of it.
#
# Input at 1/20 of an output token: prefill processes the whole prompt in
# parallel, generation is autoregressive and pays a full forward pass per token.
# The two non-degenerate fits give b/a = 16.8 (Claude 3.7 Sonnet) and 26.3 (o3);
# their geometric mean is 21.0, rounded to 20. GPT-4.1 nano's 65.1 is excluded
# from the mean because its fitted a (4.2 Wh/Mtok) is within noise of zero, and
# the two degenerate fits have no usable a at all.
#
# A cache read re-uses stored KV state instead of a fresh forward pass, so it is
# discounted to a tenth of an input token — the same 0.1x tret prices it at. A
# cache *write* is a full prefill pass, so it weighs exactly what input does.
ENERGY_TOKEN_WEIGHT_OUTPUT = Decimal("1")
ENERGY_TOKEN_WEIGHT_INPUT = Decimal("0.05")
ENERGY_CACHE_READ_MULTIPLIER = Decimal("0.005")  # 0.1 x input
ENERGY_CACHE_WRITE_MULTIPLIER = Decimal("0.05")  # 1.0 x input
# Relative-to-input ratios, kept named because that is how they are reasoned
# about (and priced) even though the absolute weights above are what the maths
# uses.
CACHE_READ_RATIO_OF_INPUT = Decimal("0.1")
CACHE_WRITE_RATIO_OF_INPUT = Decimal("1")
# The fitted output/input ratio the input weight is the reciprocal of.
OUTPUT_TO_INPUT_ENERGY_RATIO = Decimal("20")

ENERGY_TOKEN_WEIGHTS: dict[str, Decimal] = {
    "input": ENERGY_TOKEN_WEIGHT_INPUT,
    "output": ENERGY_TOKEN_WEIGHT_OUTPUT,
    "cache_read": ENERGY_CACHE_READ_MULTIPLIER,
    "cache_write": ENERGY_CACHE_WRITE_MULTIPLIER,
}

# Class defaults for catalog entries nobody has classified by hand, keyed on cost
# tier. Local models default to S (small quantized weights on end-user
# hardware), and a zero *dollar* price never means zero energy. Nothing maps to
# the reasoning class: reasoning must be marked deliberately, because price is
# not a signal for it in either direction.
_TIER_ENERGY_CLASS = {"local": "S", "economy": "M", "standard": "L", "premium": "XL"}

# ── the upgrade path, documented rather than implemented ──────────────────────
# EcoLogits models inference energy from *active* parameter count instead of a
# class ladder: Wh per output token is linear in active parameters, with an
# exponential decay in batch size, using these published fitted constants.
# tret does not implement it because no provider publishes active parameter
# counts for the closed models in the catalog — the formula would be fed a
# guess, and a precise-looking function over a guessed input is worse than an
# openly coarse bucket.
#
# The seam is `wh_per_mtok_for_model` below: give it an `active_params_b` from
# models.yaml and it can return the formula's answer instead of the class
# constant, with no other call site changing.
ECOLOGITS_ACTIVE_PARAM_MODEL = {
    "alpha": 1.17e-6,
    "beta": -1.12e-2,
    "gamma": 4.05e-5,
    "default_batch_size": 64,
    "form": "alpha * exp(beta * B) * active_params_b + gamma  (Wh per output token; per GPU, GPU energy only)",
    "source": "EcoLogits (GenAI Impact), published fitted constants",
    "url": "https://ecologits.ai/latest/methodology/llm_inference/",
}

# ── deployment split ─────────────────────────────────────────────────────────
# Only two cases matter for scope classification: did the operator buy the
# electricity (self-hosted) or did they buy a service that included it (cloud)?
DEPLOYMENT_CLOUD = "cloud"
DEPLOYMENT_LOCAL = "local"
LOCAL_PROVIDER = "local"

# PUE profiles. Separate from `deployment` because scope classification and
# facility overhead are different questions: a self-hosted model can run on a
# desk or in a company machine room, and those differ by 50% in overhead while
# landing in the same scope.
PUE_PROFILE_CLOUD = "hyperscaler_cloud"
PUE_PROFILE_WORKSTATION = "workstation"
PUE_PROFILE_ONPREM = "onprem_datacenter"
LOCAL_PUE_PROFILES = (PUE_PROFILE_WORKSTATION, PUE_PROFILE_ONPREM)

# Published PUE figures, for the record and for the provenance block.
#   Uptime Institute 2024 Global Data Center Survey (879 operators): industry
#   average 1.56 — the right figure for a generic or on-prem facility.
#   Hyperscalers self-report far better: Google 1.09 (2025 Environmental Report,
#   2024 data), AWS 1.15, Microsoft 1.16 (FY2024).
# tret's cloud default of 1.2 is therefore mildly CONSERVATIVE — above all
# three self-reports — which is the safe direction for an estimate nobody can
# verify per request.
PUE_REFERENCE = {
    "industry_average": {
        "value": 1.56,
        "source": "Uptime Institute, 2024 Global Data Center Survey (879 operators)",
        "url": "https://uptimeinstitute.com/resources/research-and-reports/uptime-institute-global-data-center-survey-results-2024",
        "date": "2024",
    },
    "google": {
        "value": 1.09,
        "source": "Google 2025 Environmental Report (FY2024 fleet-wide)",
        "url": "https://sustainability.google/reports/",
        "date": "2025",
    },
    "aws": {"value": 1.15, "source": "AWS sustainability disclosure", "date": "2024"},
    "microsoft": {"value": 1.16, "source": "Microsoft FY2024 sustainability report", "date": "2024"},
}

# ── grid intensity ───────────────────────────────────────────────────────────
# GHG Protocol Scope 2 Guidance requires distinguishing a **location-based**
# factor (the physical grid that served the load) from a **market-based** one
# (the provider's contractual renewable claims: PPAs, RECs, GOs). They are not
# interchangeable and they must not be summed. Google's published 0.03
# gCO2e/prompt is market-based and roughly 3x below its own location-based
# figure, which is exactly why the label has to travel with the number.
#
# The three labels themselves are defined in tret/config.py (Settings has to
# validate against them and config.py cannot import this module) and re-exported
# here, which is where a reader looks for what they mean.
GRID_BASIS_MEANING = {
    GRID_BASIS_LOCATION: "the physical grid that served the load",
    GRID_BASIS_MARKET: "contractual renewable claims — PPAs, RECs, GOs",
    GRID_BASIS_UNSPECIFIED: "not stated; tret will not guess a basis on your behalf",
}

# ── which rule chose the factor ──────────────────────────────────────────────
# A run records not just the grid factor it used but *why* that factor applied,
# as a stable machine-readable key. Without it a provenance table can show the
# value and not the reason, which is the more interesting half when an operator
# has configured several factors and one run looks wrong.
#
# Precedence, highest first:
#   run_override    — a factor passed straight into the accounting call. No
#                     provenance and no basis: tret was handed a number.
#   provider:<name> — TRET_GRID_FACTORS entry for the run's provider.
#   local_setting   — the legacy TRET_LOCAL_GRID_CO2E_G_PER_KWH, on a
#                     self-hosted run.
#   global_default  — TRET_GRID_CO2E_G_PER_KWH.
GRID_SOURCE_RUN_OVERRIDE = "run_override"
GRID_SOURCE_PROVIDER = "provider"
GRID_SOURCE_LOCAL_SETTING = "local_setting"
GRID_SOURCE_GLOBAL_DEFAULT = "global_default"
# The rule keys, in precedence order. A `provider:<name>` source key starts with
# GRID_SOURCE_PROVIDER + ":"; the bare word is the rule, the suffix is which
# provider matched.
GRID_SOURCE_RULES = (
    GRID_SOURCE_RUN_OVERRIDE,
    GRID_SOURCE_PROVIDER,
    GRID_SOURCE_LOCAL_SETTING,
    GRID_SOURCE_GLOBAL_DEFAULT,
)
GRID_PRECEDENCE_NOTE = (
    "Precedence: TRET_GRID_FACTORS entry for the run's provider, then "
    "TRET_LOCAL_GRID_CO2E_G_PER_KWH for a self-hosted run (legacy, still "
    "honoured), then TRET_GRID_CO2E_G_PER_KWH. A factor passed directly into the "
    "accounting call outranks all three and carries no basis claim."
)
# Why this is configuration and not geolocation. Recorded on the factor so the
# question a reviewer always asks is answered from the stored run.
GRID_NO_INFERENCE_NOTE = (
    "Operator configuration, never inference: tret does not derive a grid region "
    "from an IP address. For a cloud API call the caller's location says nothing "
    "about which data centre served the request, providers do not disclose the "
    "serving region, and a router such as OpenRouter sends the call to whichever "
    "upstream has capacity — attributing the caller's regional factor to that "
    "would be confidently arbitrary. Location is knowable when the operator knows "
    "it (they self-host somewhere, or they pin a provider to a region), so it "
    "comes from them. No network call is involved."
)


def provider_grid_source(provider: str) -> str:
    """The stable source key for a per-provider override, e.g. `provider:anthropic`."""
    return f"{GRID_SOURCE_PROVIDER}:{provider}"


def grid_source_rule(source: str | None) -> str | None:
    """The rule half of a source key: `provider:anthropic` -> `provider`."""
    if not source:
        return None
    return source.split(":", 1)[0]

GRID_REFERENCE = {
    "default": {
        "value": 458.49,
        "source": "Ember Yearly Electricity Data — World 2025 CO2 intensity",
        "url": "https://files.ember-energy.org/public-downloads/yearly_full_release_long_format.csv",
        "date": "2025",
        "note": "Lifecycle, all-GHG 100-year intensity per Ember methodology v1.5.",
        "basis": GRID_BASIS_LOCATION,
        "factor_boundary": "lifecycle_electricity_generation",
        "gas_coverage": "co2e",
        "gwp_horizon_years": 100,
        "gwp_assessment_basis": "unknown",
        "includes_td_losses": None,
        "electricity_mix_basis": "production",
        "dataset_version": "ember-yearly-2026-release",
        "observation_year": 2025,
    },
    "us_average": {
        "value": 350.0,
        "source": "US EPA eGRID2023 national average",
        "url": "https://www.epa.gov/egrid",
        "date": "2025-01",
        "note": "Subregional factors span more than 10x around this average.",
        "basis": GRID_BASIS_LOCATION,
    },
}

# Regional sourcing options, documented rather than integrated. tret
# deliberately ships **no** external API call for grid intensity: a live
# dependency in the accounting path would make a stored run's carbon figure
# depend on a third party's uptime, and every one of these sources has licence
# or coverage limits an operator has to accept for themselves. The seam is
# configuration — TRET_GRID_FACTORS (per provider), TRET_LOCAL_GRID_CO2E_G_PER_KWH
# (legacy, self-hosted) and TRET_GRID_CO2E_G_PER_KWH — into which an operator
# pastes a figure they sourced and can defend. Nothing here is geolocated.
GRID_DATA_SOURCES = (
    {
        "name": "Electricity Maps",
        "granularity": "hourly, per zone",
        "url": "https://www.electricitymaps.com/",
        "note": "Free tier is a single zone, non-commercial use only.",
    },
    {
        "name": "WattTime",
        "granularity": "marginal emissions rate, sub-hourly",
        "url": "https://watttime.org/",
        "note": "Marginal rates answer a different question than average rates.",
    },
    {
        "name": "US EPA eGRID / IEA",
        "granularity": "annual average, subregional or national",
        "url": "https://www.epa.gov/egrid",
        "note": "Annual averages; what most disclosure frameworks expect.",
    },
)

# ── embodied hardware ────────────────────────────────────────────────────────
# Cited constants for operators who want to switch embodied carbon on. Default
# stays 0 (opt-in) because tret cannot see your hardware or its lifetime.
#
# CRITICAL honesty point, and the reason `confidence` on this factor is
# "placeholder": the 273 kgCO2eq H100 figure traces to Boavizta, which states it
# could not find real GPU manufacturing LCA data and **assumed parity with
# CPU/RAM manufacturing**, and gives 30-50% margin of error on manufacturing
# footprints generally. So this is a placeholder resting on a placeholder. It is
# offered because "0" is worse, not because it is good.
EMBODIED_REFERENCE = {
    "gpu_h100_kg": 273.0,
    "server_excluding_gpus_kg": 5700.0,
    "lifetime_years": 3,
    "batch_size": 64,
    "source": "EcoLogits (GenAI Impact), embodied figures from Boavizta",
    "url": "https://ecologits.ai/latest/methodology/llm_inference/",
    "date": "2024",
    "caveat": (
        "Boavizta states it could not find GPU manufacturing LCA data and assumed "
        "parity with CPU/RAM, and gives 30-50% margin of error on manufacturing "
        "footprints. A placeholder on a placeholder."
    ),
}

# ── training amortization: excluded on purpose ───────────────────────────────
# Published per-query training amortizations span four orders of magnitude
# (~0.0001 to ~1.8 gCO2e/query) and the spread comes almost entirely from the
# assumed lifetime query count, which is unknowable from outside the provider.
# A number whose value is set by an unobservable free parameter is not an
# estimate, so tret reports it as an explicit exclusion instead of picking a
# point in a 10,000x range.
TRAINING_AMORTIZATION_EXCLUDED = (
    "Excluded. Published per-query training amortizations span ~0.0001 to ~1.8 "
    "gCO2e/query — four orders of magnitude — driven almost entirely by the "
    "assumed number of queries a model serves over its life, which providers do "
    "not disclose. Including it would require the provider's total training "
    "energy, its grid mix at training time, and a defensible lifetime query "
    "count; tret has none of the three. Inference only."
)

# ── uncertainty ──────────────────────────────────────────────────────────────
# A multiplicative *judgment band*, never a confidence interval. Nothing in this
# field publishes an interval: Green Algorithms claims order-of-magnitude
# correctness, Boavizta states 30-50%, a Sept 2025 validation study found even
# CodeCarbon (which actually measures hardware) underestimates ground truth by
# 20-30% while spec-based estimation ranges -40% to +40%, and Jegham et al. show
# a 45% swing from the batch-size assumption alone. 2.5x either way is the
# field-practice band those results support; it is a considered guess about how
# wrong this can be, not a statistic.
UNCERTAINTY_BAND_FACTOR_DEFAULT = 2.5
UNCERTAINTY_SOURCES = (
    {
        "name": "Green Algorithms",
        "claim": "order-of-magnitude correctness only",
        "source": "Lannelongue et al., Advanced Science 2021",
        "url": "https://doi.org/10.1002/advs.202100707",
        "date": "2021",
    },
    {
        "name": "Boavizta",
        "claim": "30-50% margin of error on manufacturing footprints",
        "source": "Boavizta methodology",
        "url": "https://boavizta.org/",
        "date": "2024",
    },
    {
        "name": "CodeCarbon validation",
        "claim": (
            "hardware-measuring tools underestimate ground truth by 20-30%; "
            "spec-based estimation ranges -40% to +40%, mainly cooling and PSU "
            "losses invisible to software"
        ),
        "source": "Fischer, arXiv:2509.22092",
        "url": "https://arxiv.org/abs/2509.22092",
        "date": "2025-09",
    },
    {
        "name": "Batch-size sensitivity",
        "claim": "45% swing from the batch-size assumption alone",
        "source": JEGHAM_2025["citation"],
        "url": JEGHAM_2025["url"],
        "date": JEGHAM_2025["date"],
    },
)

# ── external anchors ─────────────────────────────────────────────────────────
# Published per-prompt figures tret's output can be sanity-checked against.
# None of them is a substitute for the class ladder (they cover one model each,
# on one operator's stack) but they bound the order of magnitude, and where
# tret lands relative to them is stated in docs/emissions-methodology.md.
EXTERNAL_ANCHORS = (
    {
        "name": "Google, median Gemini text prompt",
        "energy_wh": 0.24,
        "co2e_g": 0.03,
        "water_ml": 0.26,
        "source": "Google, arXiv:2508.15734",
        "url": "https://arxiv.org/abs/2508.15734",
        "date": "2025-08-21",
        "scope": (
            "accelerator 58%, host CPU/DRAM 25%, idle and reserve capacity 10%, "
            "data-centre overhead 8%; excludes embodied hardware; carbon is "
            "market-based"
        ),
    },
    {
        "name": "Mistral, 400-token Le Chat response",
        "co2e_g": 1.14,
        "water_ml": 45.0,
        "source": (
            "Mistral AI environmental report, ISO 14040/44 + GHG Protocol Product "
            "Standard, third-party reviewed (Carbone 4, ADEME)"
        ),
        "url": "https://mistral.ai/news/our-contribution-to-a-global-environmental-standard-for-ai",
        "date": "2025-07-22",
        "scope": "full life cycle including training amortization and embodied hardware",
    },
    {
        "name": "Anthropic",
        "source": "no per-model or per-prompt energy or carbon disclosure published",
        "date": None,
        "scope": (
            "Nothing to anchor against. tret's default models are Anthropic's, so "
            "the models tret is most likely to be running are the ones with the "
            "least public data behind their energy class."
        ),
    },
    {
        "name": "Hugging Face AI Energy Score",
        "source": "~166 models benchmarked on identical H100 hardware",
        "url": "https://huggingface.co/spaces/AIEnergyScore/Leaderboard",
        "date": "2025",
        "scope": (
            "Open models only, but measured on controlled hardware — the strongest "
            "candidate to replace the class ladder for open weights."
        ),
    },
    {
        "name": "ML.ENERGY benchmark",
        "source": "arXiv:2505.06371",
        "url": "https://arxiv.org/abs/2505.06371",
        "date": "2025-05",
        "scope": "Open-weight models only.",
    },
)

# Rounding used for every persisted figure. Six decimals is well below the
# precision these estimates actually carry; it exists so the JSON is stable and
# so component figures sum exactly to their total.
_PLACES = 6

_SCOPE_BASIS = (
    "GHG Protocol scopes from the tret operator's perspective. Scope 1 = 0: "
    "running inference burns no fuel on the operator's own site, and a nonzero "
    "Scope 1 could only come from on-site generation, which tret cannot "
    "observe — so it is reported as an explicit zero rather than omitted. "
    "Scope 2 = purchased electricity for self-hosted (local) inference, where "
    "the operator buys the power. Scope 3 = cloud inference, which is a "
    "purchased service: the provider's own Scope 1/2 becomes the operator's "
    "Scope 3 (Cat. 1, purchased goods and services); plus amortized embodied "
    "hardware for local inference (Cat. 2, capital goods), which is 0 unless "
    "TRET_EMBODIED_G_PER_RUN is set. Estimated from token counts, not an "
    "inventory — docs/emissions-methodology.md."
)

_BASELINE_BASIS = (
    "Same-token counterfactual: this run's exact token counts (input, output, "
    "cache) re-priced through the baseline model's energy class. A different "
    "model would NOT produce identical token counts, so this is an efficiency "
    "indicator only — it is not an offset, not an emissions reduction or saving, "
    "not a credit, and not usable for statutory or regulatory reporting. The "
    "figure is signed: a run heavier than the baseline reports negative avoided "
    "carbon. docs/emissions-methodology.md."
)

_NO_BASELINE_BASIS = (
    "No baseline counterfactual available: no curated non-local model could be "
    "resolved (TRET_EMISSIONS_BASELINE_MODEL may name a model the catalog does "
    "not have). Reported as null rather than zero — an unavailable comparison is "
    "not a comparison that came out even."
)

_ACCOUNTING_BASIS = (
    "calibrated energy class x separately weighted input/output/cache tokens, x "
    "deployment PUE, x grid intensity; estimate, not a measurement "
    "(docs/emissions-methodology.md)"
)

_COST_BASIS = (
    "Actual token cost against the same-token baseline-model cost, from the "
    "catalog's published per-token list prices. This is the one figure here that "
    "is arithmetic rather than estimation: the prices are exact, so avoided_pct "
    "is reported to one decimal place rather than the coarse multiple used for "
    "the carbon comparison. The counterfactual is not exact — a different model "
    "would not have produced these token counts — so avoided_usd/avoided_pct are "
    "signed and are a model-selection indicator, not booked savings. Prices are "
    "list prices: published per-token rates, not a negotiated or committed-spend "
    "rate an operator may actually pay. avoided_pct is null, never 0%, when there "
    "is no baseline or the baseline itself costs nothing. This is list-price API "
    "spend only — it excludes electricity and hardware amortization for "
    "self-hosted (local) inference, so a zero-cost local run can legitimately "
    "read 100% cheaper than the frontier baseline while still carrying a real, "
    "nonzero carbon figure; see the money_excludes_self_hosting_costs caveat on "
    "such runs. Excludes everything else tret does not bill through the token "
    "API."
)

_UNCERTAINTY_BASIS = (
    "JUDGMENT BAND, NOT A CONFIDENCE INTERVAL and not a standard deviation. No "
    "credible LLM-energy methodology publishes an interval: Green Algorithms "
    "claims order-of-magnitude correctness, Boavizta states 30-50%, a 2025 "
    "validation study found hardware-measuring tools underestimate by 20-30% "
    "while spec-based estimates range -40% to +40%, and the batch-size "
    "assumption alone moves published figures by 45%. The band is a "
    "multiplicative factor applied either side of the central estimate, matching "
    "field practice. `contributions` decomposes which inputs would move the "
    "figure and by how much if that input alone were wrong; those multipliers "
    "are a sensitivity view and their product is deliberately NOT the headline "
    "band. docs/emissions-methodology.md."
)


def _f(value: Decimal | float | int, places: int = _PLACES) -> float:
    """JSON-safe rounded float. Everything persisted goes through here."""
    return float(round(Decimal(str(value)), places))


def _d(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ── energy class resolution ──────────────────────────────────────────────────
def wh_per_mtok_for_class(energy_class: str) -> Decimal:
    """Wh per million output-equivalent tokens for a class; unknown falls back to M."""
    return ENERGY_CLASS_WH_PER_MTOK.get(
        energy_class, ENERGY_CLASS_WH_PER_MTOK[DEFAULT_ENERGY_CLASS]
    )


def energy_class_for_tier(cost_tier: str) -> str:
    """Estimated energy class for an unclassified model, from its cost tier.

    Never returns the reasoning class: whether a model thinks before answering
    is not something its price tells you (the cheapest model in the reference
    dataset is also one of the two heaviest).
    """
    return _TIER_ENERGY_CLASS.get(cost_tier, DEFAULT_ENERGY_CLASS)


def is_reasoning_class(energy_class: str) -> bool:
    return energy_class == REASONING_ENERGY_CLASS


@dataclasses.dataclass(frozen=True)
class EnergyConstant:
    """The per-token energy constant a run priced its tokens at, and how it was
    reached — what `energy_accounting`'s "energy_class" factor record is built
    from, so that record can never drift from the number actually used.
    """

    wh_per_mtok: Decimal
    strategy: str  # one of emission_factors.EnergyStrategy, or "measured" for an override
    confidence: str  # exact | structural | calibrated | low | placeholder | excluded
    source: str
    url: str | None
    date: str | None
    note: str
    anchor: str | None  # e.g. "EcoLogits", or the Jegham anchor model, or None
    label: str | None  # an operator's own label for an override, else None
    interpolated: bool = False  # True only for a class with no measured anchor (XL)


def _class_ladder_note(energy_class: str) -> str:
    """The "how this class constant was reached" prose, unchanged from what
    `factor_records` has always said for the `energy_class` record — pulled
    out so both the class-ladder and (when it falls back) the active-parameter
    path can produce it identically.
    """
    calibration = ENERGY_CLASS_CALIBRATION.get(energy_class, {})
    anchor = calibration.get("anchor_model")
    note = (
        f"Least-squares fit of Wh = a x input + b x output over the three published "
        f"prompt shapes for {anchor}; b = {calibration.get('fitted_wh_per_mtok')} "
        f"Wh/Mtok is what this class is anchored on."
        if anchor
        else (
            "No measured anchor for this class: interpolated one geometric step "
            "above the class below it. The weakest constant in the ladder."
        )
    )
    note += (
        " class_ladder_v1: this constant was fitted to (or interpolated from) source "
        "observations that already include the provider's own PUE (Jegham 2025 Eq. 1); "
        "deployment PUE is applied again on top of it when this constant prices a run, "
        "so v1 double-counts facility overhead. class_ladder_v2 corrects this by "
        "normalizing to source PUE before fitting; v1 is kept only for rollback."
    )
    if is_reasoning_class(energy_class):
        note += (
            " Reasoning tier: several providers omit hidden thinking tokens from the "
            "billed output count tret reads, so energy per *visible* output token is "
            "inflated for these models. That bias is real and one-sided, not absorbed."
        )
    return note


def _class_ladder_energy_constant(
    model: ModelInfo, version: str = "class_ladder_v2"
) -> EnergyConstant:
    """Today's behavior, verbatim: an explicit `model.energy_wh_per_mtok` (set
    in `models.yaml`, or already derived from the class ladder at catalog
    construction — the two are indistinguishable by the time a run reads this
    field, and `factor_records` has never distinguished them either) wins,
    otherwise the model's calibrated class constant. Confidence, source, url,
    date and note all come from the model's `energy_class` calibration either
    way — that is `factor_records`'s existing rule, kept exactly.
    """
    energy_class = getattr(model, "energy_class", DEFAULT_ENERGY_CLASS)
    explicit = getattr(model, "energy_wh_per_mtok", None)
    if getattr(model, "energy_wh_per_mtok_explicit", False):
        return EnergyConstant(
            wh_per_mtok=_d(explicit), strategy="catalog_override", confidence="low",
            source="Explicit model catalog energy constant", url=None, date=None,
            note="Operator-supplied catalog coefficient; its physical boundary and empirical validation are unknown.",
            anchor=None, label=None,
        )
    ladder = (
        ENERGY_CLASS_WH_PER_MTOK_V1
        if version in {"class_ladder", "class_ladder_v1"}
        else ENERGY_CLASS_WH_PER_MTOK_V2
    )
    wh_per_mtok = _d(explicit) if getattr(model, "energy_wh_per_mtok_explicit", False) else ladder.get(
        energy_class, ladder[DEFAULT_ENERGY_CLASS]
    )
    calibration = ENERGY_CLASS_CALIBRATION.get(energy_class, {})
    return EnergyConstant(
        wh_per_mtok=wh_per_mtok,
        strategy=version,
        confidence="calibrated" if version != "class_ladder_v2" and calibration.get("measured") else "low",
        source=(
            JEGHAM_2025["citation"] + " — source-PUE-normalized fixed-weight fit by tret"
            if version == "class_ladder_v2"
            else JEGHAM_2025["citation"] + " — legacy least-squares fit by tret"
        ),
        url=JEGHAM_2025["url"],
        date=JEGHAM_2025["date"],
        note=(
            (
                "class_ladder_v2: no measured anchor: interpolated as L^2/M "
                "(geometric continuation); weakest constant in the ladder. Exact v1 "
                "facility observations divided by the source provider PUE, then fitted "
                "with nonnegative output + 0.05*input weighted tokens where anchored. "
                "Node-IT boundary; unused-GPU idle, facility overhead and embodied "
                "hardware excluded. Source energy is modeled from API performance and "
                "inferred hardware; deployment accuracy has not been measured."
                if calibration.get("anchor_model") is None
                else
                "class_ladder_v2: exact v1 facility observations divided by the source "
                "provider PUE, then fitted with nonnegative output + 0.05*input weighted "
                "tokens. Node-IT boundary; unused-GPU idle, facility overhead and embodied "
                "hardware excluded. Source energy is modeled from API performance and inferred "
                "hardware; deployment accuracy has not been measured."
            )
            if version == "class_ladder_v2"
            else _class_ladder_note(energy_class)
        ),
        anchor=calibration.get("anchor_model"),
        label=None,
        interpolated=version == "class_ladder_v2" and calibration.get("anchor_model") is None,
    )


def _active_params_energy_constant(active_params_b: float) -> EnergyConstant:
    """EcoLogits' published GPU energy model (`ECOLOGITS_ACTIVE_PARAM_MODEL`):

        E_gpu_per_output_token(Wh) = alpha x exp(beta x B) x active_params_b + gamma

    at `batch_size = default_batch_size` (B), converted to Wh per million
    output-equivalent tokens (x1e6) to match the ladder's unit. At tret's
    published constants and B=64 this yields (Wh/Mtok, rounded): ~44.5 (7B),
    ~80.5 (70B), ~140.5 (175B), ~271.9 (405B) — positive and monotone in
    `active_params_b` for every real model, unlike the misapplied form this
    replaced (which was negative for any plausible model and floored at the
    S-class constant for almost everything it was asked to price).

    **Scope, stated plainly — this is why confidence is `"low"`, not
    `"calibrated"`.** EcoLogits' own `f_E` is per-GPU and GPU-energy only.
    Their pipeline multiplies this per-GPU figure by however many GPUs the
    model needs (derived from total parameter count and per-GPU memory) and
    adds a separate server/host energy term before the result is comparable to
    a whole-request figure. tret has neither of those inputs — no GPU count,
    no server-energy term — and feeds this single-GPU number straight into the
    same "whole-request Wh per Mtok" slot the class ladder fills. That is a
    systematic UNDERCOUNT: 271.9 Wh/Mtok for a ~405B-active-parameter model
    sits far below the v2 L-class constant (2,399.3371 Wh/Mtok, fitted from Claude 3.7
    Sonnet — a comparably sized served model). Lifting this path to
    `"calibrated"` would need the missing GPU-count term (computable from
    total parameters and per-GPU memory, neither of which tret's catalog
    carries today) and a server/host energy term added on top — see
    docs/emissions-methodology.md's "The active-parameter formula, and its
    stated assumption".
    """
    const = ECOLOGITS_ACTIVE_PARAM_MODEL
    batch = const["default_batch_size"]
    wh_per_token = (
        const["alpha"] * math.exp(const["beta"] * batch) * active_params_b + const["gamma"]
    )
    wh_per_mtok = Decimal(str(wh_per_token * 1_000_000))
    note = (
        f"EcoLogits' published fitted GPU-energy constants ({const['form']}), at "
        f"active_params_b={active_params_b} and batch_size={batch}. This is a "
        "per-GPU, GPU-energy-only figure: EcoLogits' own pipeline multiplies it by "
        "the GPU count the model needs and adds server/host energy on top before "
        "comparing it to a whole-request figure; tret has neither term, so this "
        "systematically UNDERSTATES the run's real energy (e.g. this model's "
        f"{_f(wh_per_mtok, 1)} Wh/Mtok against the L-class 2,399.3371 Wh/Mtok fitted "
        "from a comparably sized served model). Not a measurement — see "
        "docs/emissions-methodology.md's \"The active-parameter formula, and its "
        "stated assumption\"."
    )
    return EnergyConstant(
        wh_per_mtok=wh_per_mtok,
        strategy="active_params",
        confidence="low",
        source=const["source"],
        url=const["url"],
        date=None,
        note=note,
        anchor="EcoLogits",
        label=None,
    )


def _model_override_energy_constant(override, layer: str) -> EnergyConstant:
    """A `model_overrides.<id>` entry (an `emission_factors.ModelOverride`).

    `strategy` mirrors the override's own `confidence` label rather than a
    fixed string: the whole point of `model_overrides` is an operator
    recording *how sure* they are about a number that replaces the catalog
    default, and that same word is both how it was reached and how much to
    trust it.
    """
    return EnergyConstant(
        wh_per_mtok=_d(override.energy_wh_per_mtok),
        strategy=override.confidence,
        confidence=override.confidence,
        source=override.label,
        url=override.url,
        date=override.as_of,
        note=(
            f"Operator-supplied per-model energy constant, configured at the "
            f"{layer} layer, replacing whatever the catalog default (an "
            f"explicit models.yaml constant, the active-parameter formula, or "
            f"the class ladder) would otherwise have resolved for this model."
        ),
        anchor=None,
        label=override.label,
    )


def _energy_constant_and_flags(
    model: ModelInfo, factors: "FactorSet | None"
) -> tuple[EnergyConstant, bool]:
    """`(EnergyConstant, active_params_unknown)` — the second element is `True`
    only when `factors.energy_strategy` asked for the active-parameter formula
    and this model has no `active_params_b` to feed it, i.e. exactly when the
    `active_params_unknown` caveat belongs on the run.

    Resolution order — first one that applies wins:

    1. `factors.model_override` — an operator's `model_overrides` entry for
       *this* model id, at whichever layer set it.
    2. `model.energy_wh_per_mtok`, but *only* when it was set explicitly (a
       real `models.yaml` constant) rather than baked in by
       `ModelInfo.__post_init__`'s own class-ladder derivation — see
       `ModelInfo.energy_wh_per_mtok_explicit`. The field itself is never
       `None` after construction, explicit or not, so testing it against
       `None` here would make this rung fire for every model and make rung 3
       below unreachable.
    3. The EcoLogits active-parameter formula, when `factors.energy_strategy`
       is `"active_params"` and `model.active_params_b` is set.
    4. The class ladder again, as the fallback for (3) not applying — no
       explicit constant and either the strategy is not `active_params` or
       the model has no `active_params_b`.
    """
    if factors is not None and factors.model_override is not None:
        resolved = factors.model_override
        return _model_override_energy_constant(resolved.value, resolved.layer), False

    strategy = factors.energy_strategy.value if factors is not None else "class_ladder_v2"
    ladder_version = strategy if strategy in {"class_ladder", "class_ladder_v1", "class_ladder_v2"} else "class_ladder_v2"
    ladder = _class_ladder_energy_constant(model, ladder_version)
    if getattr(model, "energy_wh_per_mtok_explicit", False):
        return ladder, False

    active_params_b = getattr(model, "active_params_b", None)
    if strategy == "active_params":
        if active_params_b is not None:
            return _active_params_energy_constant(active_params_b), False
        return ladder, True

    return ladder, False


def energy_constant_for_model(
    model: ModelInfo, factors: "FactorSet | None" = None
) -> EnergyConstant:
    """The full provenance of this run's per-token energy constant. See
    `_energy_constant_and_flags` for the resolution order; `energy_accounting`
    calls that directly so it can also see the `active_params_unknown` flag.
    """
    constant, _ = _energy_constant_and_flags(model, factors)
    return constant


def wh_per_mtok_for_model(model: ModelInfo, *, factors: "FactorSet | None" = None) -> Decimal:
    """The per-model energy constant a run should price tokens at.

    Positional-compatible with every existing caller (`factors` is
    keyword-only and defaults to `None`, which reproduces the exact ladder
    this function has always used — see `_class_ladder_energy_constant`).
    Given a `FactorSet`, this is the seam described in
    docs/emissions-methodology.md's "Energy strategies and measured runs":
    a `model_overrides` entry or the active-parameter formula can now win
    instead of the class ladder. See `energy_constant_for_model` for the full
    provenance (strategy, confidence, source) behind the number returned here.
    """
    return energy_constant_for_model(model, factors).wh_per_mtok


# ── token weighting ──────────────────────────────────────────────────────────
def weighted_tokens(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Tokens weighted by how much forward-pass work each bucket really costs.

    The unit is an **output-equivalent token**: generation is the expensive
    operation, so an output token is 1.0 and prefill work is a fraction of it
    (input 0.05, cache write 0.05, cache read 0.005). Multiplying this by a
    class constant in Wh/Mtok gives compute energy, exactly as before — what
    changed is that reading a token and writing one are no longer priced the
    same, because measurement says they are ~20x apart.
    """
    return (
        ENERGY_TOKEN_WEIGHT_INPUT * Decimal(input_tokens)
        + ENERGY_TOKEN_WEIGHT_OUTPUT * Decimal(output_tokens)
        + ENERGY_CACHE_READ_MULTIPLIER * Decimal(cache_read_tokens)
        + ENERGY_CACHE_WRITE_MULTIPLIER * Decimal(cache_write_tokens)
    )


def energy_wh_by_bucket(
    wh_per_mtok: Decimal | float,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> dict[str, Decimal]:
    """Compute (IT-load) Wh split per token bucket. Sums to `energy_wh`.

    Exposed because the input/output split is the single biggest change to this
    model, and a reader should be able to see that a long-prompt run is mostly
    generation energy rather than take it on faith.
    """
    per_mtok = _d(wh_per_mtok)
    counts = {
        "input": Decimal(input_tokens),
        "output": Decimal(output_tokens),
        "cache_read": Decimal(cache_read_tokens),
        "cache_write": Decimal(cache_write_tokens),
    }
    return {
        bucket: per_mtok * ENERGY_TOKEN_WEIGHTS[bucket] * count / Decimal(1_000_000)
        for bucket, count in counts.items()
    }


def co2e_grams(energy_wh: Decimal, grid_g_per_kwh: float | None = None) -> Decimal:
    """gCO2e for an energy figure, at the configured grid intensity.

    Takes energy as given: pass the *total* (PUE-inclusive) figure if that is
    what you mean to convert. Unchanged from the original contract.
    """
    if grid_g_per_kwh is None:
        grid_g_per_kwh = get_settings().grid_co2e_g_per_kwh
    return energy_wh * Decimal(str(grid_g_per_kwh)) / Decimal(1000)


# ── deployment, overhead, grid factors ───────────────────────────────────────
def deployment_for(provider: str) -> str:
    """"local" for self-hosted inference, "cloud" for everything else.

    The distinction is who pays the power bill, which is what decides the scope
    the emissions land in.
    """
    return DEPLOYMENT_LOCAL if provider == LOCAL_PROVIDER else DEPLOYMENT_CLOUD


def pue_profile_for(deployment: str, settings: Settings | None = None) -> str:
    """Which facility the PUE figure is supposed to describe.

    Cloud is always the hyperscaler profile. Self-hosted splits, because
    "local" covers both a workstation under a desk (almost no facility
    overhead) and a company machine room (which is a small data centre and
    should carry the industry-average figure, not a hyperscaler's).
    """
    settings = settings or get_settings()
    if deployment != DEPLOYMENT_LOCAL:
        return PUE_PROFILE_CLOUD
    profile = (settings.local_deployment_profile or "").strip().lower()
    return profile if profile in LOCAL_PUE_PROFILES else PUE_PROFILE_WORKSTATION


def pue_for(deployment: str, settings: Settings | None = None) -> Decimal:
    """Power Usage Effectiveness: total facility energy / IT-load energy.

    Resolved per deployment profile, with the source recorded on the run:

    * `hyperscaler_cloud` — 1.2. Google self-reports 1.09 fleet-wide (2025
      Environmental Report, 2024 data), AWS 1.15, Microsoft 1.16 (FY2024), so
      1.2 sits *above* all three: mildly conservative, which is the safe
      direction for a facility tret cannot see.
    * `workstation` — 1.05, for fans and a share of room cooling on a desktop.
    * `onprem_datacenter` — 1.56, the Uptime Institute 2024 industry average
      across 879 operators. A generic or on-prem facility is nothing like a
      hyperscaler campus and must not borrow its number.
    """
    settings = settings or get_settings()
    profile = pue_profile_for(deployment, settings)
    if profile == PUE_PROFILE_ONPREM:
        raw = settings.onprem_pue
    elif profile == PUE_PROFILE_WORKSTATION:
        raw = settings.local_pue
    else:
        raw = settings.datacenter_pue
    pue = Decimal(str(raw))
    # A PUE below 1 is physically impossible (you cannot use less than the IT
    # load); refuse to let a misconfiguration shrink the number.
    return pue if pue >= 1 else Decimal(1)


def normalize_grid_basis(raw: str | None) -> str:
    """A recorded basis label, or `unspecified` for anything unrecognised."""
    basis = (raw or "").strip().lower()
    return basis if basis in GRID_BASES else GRID_BASIS_UNSPECIFIED


def grid_factor_override_for(
    provider: str | None, settings: Settings | None = None
) -> tuple[str, GridFactor] | None:
    """(provider name, entry) from `TRET_GRID_FACTORS`, or None.

    Keyed on the tret provider name, which is the only thing tret actually
    knows about where a request went. An entry naming a provider the catalog does
    not have simply never matches — it warned at startup and is otherwise inert.
    """
    if not provider:
        return None
    settings = settings or get_settings()
    name = provider.strip().lower()
    entry = (settings.grid_factors or {}).get(name)
    return (name, entry) if entry is not None else None


def resolve_grid_factor(
    provider: str | None,
    deployment: str | None = None,
    settings: Settings | None = None,
    *,
    override: float | None = None,
) -> dict[str, Any]:
    """Which grid factor applies, and — recorded on the run — *why*.

    Returns `{value, basis, source, rule, provider, label, setting}` where
    `source` is the stable machine-readable key (`provider:anthropic`,
    `local_setting`, `global_default`, `run_override`) and `label` is the
    operator's own note about the factor, when they set one.

    The precedence is `GRID_PRECEDENCE_NOTE`, and it is deliberately ordered
    most-specific-first: an operator who has configured a factor for a provider
    has said something more precise than either the legacy local setting or the
    global default, so it wins. The legacy `local_grid_*` settings keep working
    exactly as they always have for any provider with no entry of its own.

    `provider=None` skips the per-provider lookup entirely, which is what makes
    every pre-existing call site behave as it did before this existed.
    """
    settings = settings or get_settings()
    if deployment is None:
        deployment = deployment_for(provider) if provider else DEPLOYMENT_CLOUD

    if override is not None:
        # A number handed to the accounting call. tret cannot state its
        # provenance and must not borrow a basis from a setting that was not used.
        return {
            "value": float(override),
            "basis": GRID_BASIS_UNSPECIFIED,
            "source": GRID_SOURCE_RUN_OVERRIDE,
            "rule": GRID_SOURCE_RUN_OVERRIDE,
            "provider": provider,
            "label": None,
            "setting": None,
        }

    entry = grid_factor_override_for(provider, settings)
    if entry is not None:
        name, factor = entry
        return {
            "value": float(factor.g_per_kwh),
            "basis": normalize_grid_basis(factor.basis),
            "source": provider_grid_source(name),
            "rule": GRID_SOURCE_PROVIDER,
            "provider": name,
            "label": factor.label,
            "setting": f"TRET_GRID_FACTORS[{name}]",
        }

    if deployment == DEPLOYMENT_LOCAL and settings.local_grid_co2e_g_per_kwh is not None:
        return {
            "value": float(settings.local_grid_co2e_g_per_kwh),
            "basis": normalize_grid_basis(settings.local_grid_co2e_basis),
            "source": GRID_SOURCE_LOCAL_SETTING,
            "rule": GRID_SOURCE_LOCAL_SETTING,
            "provider": provider,
            "label": None,
            "setting": "TRET_LOCAL_GRID_CO2E_G_PER_KWH",
        }

    return {
        "value": float(settings.grid_co2e_g_per_kwh),
        "basis": normalize_grid_basis(settings.grid_co2e_basis),
        "source": GRID_SOURCE_GLOBAL_DEFAULT,
        "rule": GRID_SOURCE_GLOBAL_DEFAULT,
        "provider": provider,
        "label": None,
        "setting": "TRET_GRID_CO2E_G_PER_KWH",
    }


def grid_factor_for(
    deployment: str, settings: Settings | None = None, *, provider: str | None = None
) -> float:
    """gCO2e/kWh to apply to this deployment's electricity.

    A per-provider `TRET_GRID_FACTORS` entry wins when `provider` is given.
    Self-hosted inference may otherwise use the operator's own site/market-based
    factor (`local_grid_co2e_g_per_kwh`); unset, it falls back to the single
    `grid_co2e_g_per_kwh`. Cloud inference with no entry of its own always uses
    `grid_co2e_g_per_kwh`, since tret does not know which region served the
    request and refuses to infer one.
    """
    return float(resolve_grid_factor(provider, deployment, settings)["value"])


def grid_basis_for(
    deployment: str,
    settings: Settings | None = None,
    *,
    overridden: bool = False,
    provider: str | None = None,
) -> str:
    """The GHG Protocol basis label for the factor actually applied.

    A location-based factor describes the physical grid; a market-based one
    describes contractual renewable claims. They answer different questions and
    may not be mixed or summed, so the label is recorded per run rather than
    assumed. An explicitly passed factor reports `unspecified`: tret was handed
    a number, not a provenance.
    """
    if overridden:
        return GRID_BASIS_UNSPECIFIED
    return str(resolve_grid_factor(provider, deployment, settings)["basis"])


def embodied_g_for(deployment: str, settings: Settings | None = None) -> Decimal:
    """Amortized embodied (capital-goods) carbon per run, in grams.

    Only self-hosted inference gets one: it is the operator's own hardware, so
    manufacturing it is their Scope 3 Cat. 2. Cloud hardware is embedded in the
    purchased service and is not separately estimated (tret has no basis for
    it). Defaults to 0, which *understates* local inference — see the docs and
    `amortized_embodied_g_per_run` for cited constants to set it from.
    """
    settings = settings or get_settings()
    if deployment != DEPLOYMENT_LOCAL:
        return Decimal(0)
    return max(Decimal(str(settings.embodied_g_per_run)), Decimal(0))


def amortized_embodied_g_per_run(
    runs_over_lifetime: int,
    gpus: int = 1,
    *,
    include_server: bool = True,
    batch_size: int = 64,
) -> Decimal:
    """A cited starting point for `TRET_EMBODIED_G_PER_RUN`, in grams.

    The convention, taken from EcoLogits so the number is comparable with
    published figures: NVIDIA H100 = 273 kgCO2eq per unit, server chassis
    excluding GPUs = 5,700 kgCO2eq, amortized over a 3-year hardware lifetime at
    batch size 64 — a run shares the hardware with `batch_size - 1` others, so
    it carries only its share.

    Nothing calls this in the accounting path. It exists so the doc's worked
    example and the code agree, and so an operator can compute a defensible
    figure instead of guessing one. Read `EMBODIED_REFERENCE["caveat"]` first:
    the GPU constant is a placeholder resting on a placeholder.
    """
    if runs_over_lifetime <= 0 or batch_size <= 0:
        return Decimal(0)
    kg = _d(EMBODIED_REFERENCE["gpu_h100_kg"]) * Decimal(max(gpus, 0))
    if include_server:
        kg += _d(EMBODIED_REFERENCE["server_excluding_gpus_kg"])
    grams = kg * Decimal(1000)
    return grams / (Decimal(runs_over_lifetime) * Decimal(batch_size))


# ── uncertainty ──────────────────────────────────────────────────────────────
def band_factors(settings: Settings | None = None) -> tuple[Decimal, Decimal]:
    """(low divisor, high multiplier) for the judgment band, both >= 1.

    Clamped at 1: a factor below 1 would invert the band and report a "low"
    above the central estimate, which is not a band, it is a bug.
    """
    settings = settings or get_settings()
    low = max(Decimal(str(settings.uncertainty_band_low)), Decimal(1))
    high = max(Decimal(str(settings.uncertainty_band_high)), Decimal(1))
    return low, high


def _band(value: Decimal, low: Decimal, high: Decimal) -> tuple[Decimal, Decimal]:
    """value/low .. value*high, never below zero."""
    central = max(value, Decimal(0))
    return central / low, central * high


def uncertainty_contributions(
    *, reasoning_tier: bool, deployment: str, measured: bool = False
) -> list[dict]:
    """Per-factor sensitivity: what moves if this input alone is wrong.

    Deliberately NOT combined into the headline band. Multiplying these together
    would give a band far wider than any published methodology claims, and
    presenting that as the answer would be its own kind of dishonesty. Grid
    intensity and the energy class dominate; the rest are second order.

    `measured=True` (an operator-supplied `measured_energy_wh` on this run)
    narrows `energy_class` and `batching` to instrument-level tolerance — the
    class/batch-size guesswork those two rows exist to bound no longer applies
    once the actual IT-load energy was metered rather than modeled from token
    counts. The headline band itself is unchanged this phase; only these two
    per-factor rows narrow.
    """
    out = [
        {
            "key": "grid_intensity",
            "label": "Grid carbon intensity",
            "low_multiplier": 0.06,
            "high_multiplier": 1.6,
            "dominant": True,
            "note": (
                "A regional factor ranges from ~30 gCO2e/kWh (Sweden) to ~750+ "
                "(coal-heavy); eGRID subregions span more than 10x. A market-based "
                "factor can be ~3x below the location-based one for the same load."
            ),
        },
        {
            "key": "energy_class",
            "label": "Model energy class",
            "low_multiplier": 0.33,
            "high_multiplier": 3.0,
            "dominant": True,
            "note": (
                "Classes are ~2-5x apart and assignment for closed models is "
                "judgement from price and tier, so being one class out is the "
                "expected failure mode."
            ),
        },
        {
            "key": "batching",
            "label": "Batch size at serving time",
            "low_multiplier": 0.55,
            "high_multiplier": 1.45,
            "dominant": False,
            "note": "Jegham et al. report a 45% swing from the batch-size assumption alone.",
        },
        {
            "key": "measurement_bias",
            "label": "Spec-based estimation bias",
            "low_multiplier": 0.6,
            "high_multiplier": 1.4,
            "dominant": False,
            "note": (
                "Fischer 2025: spec-based estimation ranges -40% to +40% against "
                "ground truth, mostly cooling and PSU losses invisible to software."
            ),
        },
        {
            "key": "pue",
            "label": "Facility overhead (PUE)",
            "low_multiplier": 0.91,
            "high_multiplier": 1.3,
            "dominant": False,
            "note": (
                "1.09 (Google self-reported) to 1.56 (Uptime Institute industry "
                "average) against tret's 1.2 cloud default."
            ),
        },
        {
            "key": "token_energy_ratio",
            "label": "Output/input energy ratio",
            "low_multiplier": 0.9,
            "high_multiplier": 1.15,
            "dominant": False,
            "note": (
                "The fitted ratio is 16.8 (Claude 3.7 Sonnet) to 26.3 (o3), applied "
                "as 20. The effect on a run depends on its input/output mix."
            ),
        },
        {
            "key": "reasoning_tokens",
            "label": "Hidden reasoning tokens",
            "low_multiplier": 1.0,
            "high_multiplier": 3.0 if reasoning_tier else 1.2,
            "dominant": bool(reasoning_tier),
            "note": (
                "One-sided: several providers exclude hidden thinking tokens from "
                "the billed output count tret reads, so real generation work can "
                "only be higher than counted, never lower."
            ),
        },
    ]
    if deployment == DEPLOYMENT_LOCAL:
        out.append(
            {
                "key": "unbatched_local_inference",
                "label": "Unbatched single-user inference",
                "low_multiplier": 1.0,
                "high_multiplier": 5.0,
                "dominant": True,
                "note": (
                    "One-sided: the class constants are calibrated on batched "
                    "serving stacks. A single-user local model amortizes weight "
                    "loading over one request instead of dozens, so per-token "
                    "energy is materially higher than class S implies."
                ),
            }
        )
    if measured:
        for c in out:
            if c["key"] in ("energy_class", "batching"):
                c["low_multiplier"] = 0.9
                c["high_multiplier"] = 1.1
                c["dominant"] = False
                c["note"] = (
                    "Measured: this run's IT-load energy was operator-supplied "
                    "rather than estimated from token counts, so the usual "
                    "class/batch-size uncertainty narrows to instrument-level "
                    "tolerance. " + c["note"]
                )
    return out


def uncertainty_band(
    co2e_g: Decimal,
    energy_wh: Decimal,
    energy_wh_total: Decimal,
    *,
    reasoning_tier: bool,
    deployment: str,
    settings: Settings | None = None,
    band: tuple[Decimal, Decimal] | None = None,
    measured: bool = False,
    evidence: "Evidence | None" = None,
    pue_applied: bool = True,
) -> dict:
    """The judgment band around a run's figures. Never negative, never a CI.

    `band`, when given, is the already-resolved `(low, high)` pair — a
    `FactorSet`'s, so a layered override actually reaches the arithmetic here
    rather than only the provenance record. Recomputed from `settings` via
    `band_factors` when omitted, exactly as before. `measured` narrows the
    per-factor `energy_class`/`batching` contributions (see
    `uncertainty_contributions`); the headline band itself is untouched by
    `measured` alone.

    `evidence` (`tret.services.uncertainty_derivation.Evidence`), when given,
    switches the headline band itself: `contributions` are narrowed per
    `adjust_contributions`, and `derive_band` folds the (adjusted) rows into
    a band bounded above by the *configured* `band` (or `band_factors`) —
    never wider than it, only ever at or inside it. The response then gains
    a `derivation` key (`band_record`'s output) recording the configured
    band alongside the one actually applied and which row (if any) governed
    it. `evidence=None` (every caller before this parameter existed) is
    exactly today's behaviour: the configured band applies untouched and
    there is no `derivation` key.
    """
    configured_low, configured_high = band if band is not None else band_factors(settings)
    contributions = uncertainty_contributions(
        reasoning_tier=reasoning_tier, deployment=deployment,
        measured=measured and evidence is not None and evidence.energy_measured,
    )
    if not pue_applied:
        for contribution in contributions:
            if contribution["key"] == "pue":
                contribution.update({
                    "low_multiplier": 1.0,
                    "high_multiplier": 1.0,
                    "dominant": False,
                    "note": "PUE was not applied at this recorded energy boundary.",
                })
    low, high = configured_low, configured_high
    derivation = None
    if evidence is not None:
        contributions = adjust_contributions(contributions, evidence)
        derived = derive_band(
            contributions, configured_low=configured_low, configured_high=configured_high
        )
        low, high = derived.low, derived.high
        derivation = band_record(
            derived, configured_low=configured_low, configured_high=configured_high
        )

    co2e_low, co2e_high = _band(co2e_g, low, high)
    wh_low, wh_high = _band(energy_wh, low, high)
    total_low, total_high = _band(energy_wh_total, low, high)
    result = {
        "kind": "judgment_band",
        # Named explicitly so no downstream renderer can label this a CI by
        # accident. It is the single most important field in the block.
        "is_confidence_interval": False,
        "band_factor_low": _f(low, 3),
        "band_factor_high": _f(high, 3),
        "co2e_g_low": _f(co2e_low),
        "co2e_g_high": _f(co2e_high),
        "energy_wh_low": _f(wh_low),
        "energy_wh_high": _f(wh_high),
        "energy_wh_total_low": _f(total_low),
        "energy_wh_total_high": _f(total_high),
        "contributions": contributions,
        "basis": _UNCERTAINTY_BASIS,
    }
    if derivation is not None:
        result["derivation"] = derivation
    return result


def _band_evidence(
    factors: "FactorSet", validated: "Evidence | None"
) -> "Evidence | None":
    """Use caller-validated evidence only when derived bands are enabled.

    Factor labels, dates and profile names are provenance, not validation.
    They therefore never create narrowing evidence automatically — which layer
    resolved a PUE/grid win is not itself a reason to narrow the band, so
    nothing here inspects `factors.layers_present` or a factor's own layer.
    """
    if not (factors.band_low.derived or factors.band_high.derived):
        return None
    return validated or Evidence()


# ── provenance ───────────────────────────────────────────────────────────────
# A **list**, not a dict, on purpose: `runs.energy_accounting` is JSONB and
# Postgres does not preserve object key order, so a dict would render the
# provenance table in a different order on every read. Each entry carries its own
# `key` for lookup.
def _factor(
    key: str,
    label: str,
    value,
    unit: str | None,
    source: str,
    url: str | None,
    date: str | None,
    confidence: str,
    note: str,
    setting: str | None = None,
    **extra,
) -> dict:
    """One provenance record. `confidence` is one of:

    exact       — arithmetic on a published price; no estimation involved
    structural  — follows from how inference works, not from a measurement
    calibrated  — fitted to measured data, generalised beyond it
    low         — judgement anchored on something published
    placeholder — a stand-in the source itself describes as unsupported
    excluded    — deliberately not counted, with the reason
    """
    return {
        "key": key,
        "label": label,
        "value": value,
        "unit": unit,
        "source": source,
        "url": url,
        "date": date,
        "confidence": confidence,
        "note": note,
        "setting": setting,
        **extra,
    }


def factor_records(
    *,
    energy_class: str,
    deployment: str,
    settings: Settings,
    factors: "FactorSet",
    energy_constant: "EnergyConstant",
    energy_layer: str,
    energy_setting: str | None,
    measured_energy: bool = False,
) -> list[dict]:
    """Every constant that went into this run, with where it came from.

    The point of this list is that a reader of a stored run can answer "where did
    this number come from" for every input without consulting the code, the docs,
    or a frontend lookup table. It is generated from the same constants the
    arithmetic uses, so it cannot drift from them.

    `factors` (a `tret.services.emission_factors.FactorSet`) carries the
    already-resolved value, layer and setting path for grid/PUE/embodied/band —
    the arguments this function used to take individually and either compute
    itself (`band_factors(settings)`) or receive pre-computed. Reading them off
    `factors` instead means a layered override (a workspace's own PUE, say)
    shows up here exactly as it showed up in the arithmetic, not recomputed
    from `settings` alone and quietly out of step with it.

    `energy_constant` is the resolved `EnergyConstant` (see
    `energy_constant_for_model`) the `energy_class` record below is built from;
    `energy_layer` / `energy_setting` are its layer and setting path — computed
    by the caller because they depend on *which* rule won (a `model_overrides`
    layer, the strategy setting's layer, or `global_default` for the plain
    class ladder), not on anything this function itself resolves.
    `measured_energy=True` (an operator-supplied `measured_energy_wh` on this
    run) overrides that record's confidence/source/note regardless of
    `energy_constant.strategy`: what was actually recorded is a real
    measurement, not whichever estimate produced it.
    """
    # Lazy: `emission_factors` imports plain functions from this module at its
    # own top level, so this module cannot import it back at *its* top level
    # without a circular import. By the time any caller actually runs this
    # function, both modules have finished loading.
    from tret.services.emission_factors import (
        LAYER_DATASET,
        LAYER_ENV,
        LAYER_GLOBAL_DEFAULT,
        LAYER_PRECEDENCE,
        LAYER_RUN_OVERRIDE,
    )

    pue = factors.pue.value
    pue_profile = factors.pue_profile
    grid = float(factors.grid.value)
    grid_basis = factors.grid_basis
    grid_overridden = factors.grid.layer == LAYER_RUN_OVERRIDE
    grid_source = factors.grid.source
    grid_source_label = factors.grid.label
    grid_setting = factors.grid.setting
    embodied_g = factors.embodied_g.value
    low, high = factors.band_low.value, factors.band_high.value
    band_layer = (
        factors.band_low.layer
        if LAYER_PRECEDENCE.index(factors.band_low.layer)
        <= LAYER_PRECEDENCE.index(factors.band_high.layer)
        else factors.band_high.layer
    )
    # A run-override source string is textually identical to the layer name
    # ("run_override"); every other layer's source for grid carries its own
    # citation-friendly text (see `emission_factors._layer_source`), and the
    # provider a workspace/managed source names, when it names one.
    provider_match = (
        grid_source.split(":provider:")[-1] if ":provider:" in grid_source else None
    )
    pue_ref = {
        PUE_PROFILE_CLOUD: PUE_REFERENCE["google"],
        PUE_PROFILE_ONPREM: PUE_REFERENCE["industry_average"],
    }.get(pue_profile)
    grid_ref = GRID_REFERENCE["default"]

    # The "energy_class" record's confidence/source/note/date/url come from
    # `energy_constant` — which, for the plain class-ladder path, is exactly
    # the calibration lookup this record has always used (see
    # `_class_ladder_energy_constant`); `strategy` is new, and `measured_energy`
    # overrides confidence/source/note again on top, whichever strategy priced
    # the (now-superseded) estimate.
    energy_note = energy_constant.note
    energy_confidence = energy_constant.confidence
    energy_source = energy_constant.source
    energy_label = f"Energy class {energy_class}"
    if energy_constant.strategy != "class_ladder":
        energy_label += f" ({energy_constant.label or energy_constant.strategy})"
    if measured_energy:
        energy_confidence = "measured"
        energy_source = "Operator-supplied measurement (IT-load Wh)"
        energy_note = (
            "This run's actual energy was operator-measured directly (IT-load "
            "Wh) and replaces the per-token estimate below, which is still "
            "recorded separately as energy_wh_estimated. What would otherwise "
            "have been estimated: " + energy_note
        )

    pue_note = {
        PUE_PROFILE_CLOUD: (
            "1.2 for hyperscaler cloud. Above every self-report (Google 1.09, "
            "AWS 1.14) and well below the 1.56 industry average, i.e. "
            "deliberately conservative for a facility tret cannot see. "
            "Self-reported figures are fleet averages, not the building "
            "that served this request."
        ),
        PUE_PROFILE_WORKSTATION: (
            "1.05 for a desktop or workstation: fans and a share of room "
            "cooling, no facility to speak of."
        ),
        PUE_PROFILE_ONPREM: (
            "1.56, the Uptime Institute 2024 industry average across 879 "
            "operators. An on-prem machine room is a small data centre and "
            "must not borrow a hyperscaler's number."
        ),
    }[pue_profile]
    if factors.pue.disclosure:
        pue_note = (
            f"Provider-asserted {factors.pue.disclosure['statistic'].replace('_', ' ')} "
            f"for {factors.pue.label}; it is not a measurement of this request."
        )

    factors = [
        _factor(
            "energy_class",
            energy_label,
            _f(energy_constant.wh_per_mtok, 3),
            "Wh per million output-equivalent tokens",
            energy_source,
            energy_constant.url,
            energy_constant.date,
            energy_confidence,
            energy_note,
            energy_setting,
            anchor_model=energy_constant.anchor,
            measured_anchor=energy_confidence in ("measured", "calibrated"),
            reasoning_tier=is_reasoning_class(energy_class),
            strategy="measured" if measured_energy else energy_constant.strategy,
            layer=energy_layer,
            measured=measured_energy,
            # True only for a class with no measured anchor (XL under v2):
            # interpolated as L^2/M rather than fitted. False for every
            # anchored class, and for a measured/overridden constant.
            interpolated=energy_constant.interpolated,
        ),
        _factor(
            "token_weight_output",
            "Output token weight",
            _f(ENERGY_TOKEN_WEIGHT_OUTPUT, 4),
            "output-equivalent tokens per output token",
            "definition of the unit",
            None,
            None,
            "structural",
            "The unit of the energy class is one generated token, so this is 1 by definition.",
            layer=LAYER_GLOBAL_DEFAULT,
        ),
        _factor(
            "token_weight_input",
            "Input token weight",
            _f(ENERGY_TOKEN_WEIGHT_INPUT, 4),
            "output-equivalent tokens per input token",
            JEGHAM_2025["citation"] + " — fitted b/a ratio",
            JEGHAM_2025["url"],
            JEGHAM_2025["date"],
            "calibrated",
            (
                "1/20. Prefill is parallel, generation is autoregressive. The two "
                "non-degenerate fits give b/a = 16.8 and 26.3 (geometric mean 21.0); "
                "the other three fits are degenerate or near-zero in a, so 20 is "
                "applied as a documented assumption across all classes."
            ),
            layer=LAYER_GLOBAL_DEFAULT,
        ),
        _factor(
            "token_weight_cache_read",
            "Cache-read token weight",
            _f(ENERGY_CACHE_READ_MULTIPLIER, 4),
            "output-equivalent tokens per cached token read",
            "tret, by analogy with prompt-cache pricing",
            None,
            None,
            "structural",
            (
                "A tenth of an input token: a read re-uses stored KV state instead of a "
                "fresh forward pass, mirroring the 0.1x providers charge. Not zero — "
                "the state still has to be fetched and attended over."
            ),
            layer=LAYER_GLOBAL_DEFAULT,
        ),
        _factor(
            "token_weight_cache_write",
            "Cache-write token weight",
            _f(ENERGY_CACHE_WRITE_MULTIPLIER, 4),
            "output-equivalent tokens per cached token written",
            "tret, structural",
            None,
            None,
            "structural",
            "A cache write is a full prefill pass, so it weighs exactly what input does.",
            layer=LAYER_GLOBAL_DEFAULT,
        ),
        _factor(
            "pue",
            "Power Usage Effectiveness",
            _f(pue, 3),
            "total facility energy / IT-load energy",
            factors.pue.label
            or (pue_ref or {}).get("source", "tret default for a workstation profile"),
            factors.pue.url or (pue_ref or {}).get("url"),
            factors.pue.as_of or (pue_ref or {}).get("date"),
            "low",
            pue_note,
            factors.pue.setting,
            profile=pue_profile,
            layer=factors.pue.layer,
            disclosure=factors.pue.disclosure,
        ),
        _factor(
            "grid_intensity",
            "Grid carbon intensity",
            float(grid),
            "gCO2e/kWh",
            # Only claim Ember as the source when the shipped default was actually
            # applied. Equal numeric values supplied by an operator remain custom.
            (
                grid_ref["source"]
                if grid_source == GRID_SOURCE_GLOBAL_DEFAULT
                and not grid_overridden
                and float(grid) == grid_ref["value"]
                else f"published — {grid_source_label}"
                if factors.grid.layer == LAYER_DATASET
                else f"operator-supplied — {grid_source_label}"
                if grid_source_label
                else "operator-supplied"
            ),
            (
                factors.grid.url
                if factors.grid.layer == LAYER_DATASET
                else grid_ref["url"]
                if factors.grid.layer == LAYER_GLOBAL_DEFAULT
                else None
            ),
            (
                factors.grid.as_of
                if factors.grid.layer == LAYER_DATASET
                else grid_ref["date"]
                if factors.grid.layer == LAYER_GLOBAL_DEFAULT
                else None
            ),
            "low",
            (
                (
                    "Explicitly supplied for this run, so tret cannot state its "
                    "provenance or its GHG Protocol basis. "
                )
                if grid_overridden
                else (
                    "Published yearly average for "
                    + (
                        f"country {grid_source.rsplit(':', 1)[-1]} (Ember, CC BY 4.0)"
                        if grid_source.startswith("dataset:ember:")
                        else f"grid zone {grid_source.rsplit(':', 1)[-1]} (Electricity Maps, ODbL)"
                    )
                    + ", applied "
                    "because this workspace pinned the provider to region "
                    f"{factors.grid.region} — a pin the operator made, not a region tret "
                    "inferred. "
                )
                if factors.grid.layer == LAYER_DATASET
                else (
                    f"Configured at the {factors.grid.layer} layer"
                    + (f" for provider {provider_match}" if provider_match else "")
                    + ", which outranks the environment-level default and the shipped "
                    "Ember World average for this run. "
                )
                if factors.grid.layer not in (LAYER_ENV, LAYER_GLOBAL_DEFAULT)
                else (
                    "Configured per provider"
                    + (
                        f" ({grid_source.split(':', 1)[1]})"
                        if ":" in grid_source
                        else ""
                    )
                    + ", which outranks both the self-hosted setting and the global "
                    "default for this run's provider. "
                )
                if grid_source_rule(grid_source) == GRID_SOURCE_PROVIDER
                else (
                    "The operator's self-hosted factor (TRET_LOCAL_GRID_CO2E_G_PER_KWH, "
                    "the legacy setting), applied because this run ran locally and its "
                    "provider has no TRET_GRID_FACTORS entry. "
                )
                if grid_source == GRID_SOURCE_LOCAL_SETTING
                else (
                    f"The shipped default is Ember's World 2025 value, "
                    f"{grid_ref['value']} gCO2e/kWh. "
                )
                if factors.grid.layer == LAYER_GLOBAL_DEFAULT
                else (
                    "Configured by the operator, replacing the shipped Ember World "
                    f"average of {grid_ref['value']} gCO2e/kWh. "
                )
            )
            + (
                f"Basis: {grid_basis} — {GRID_BASIS_MEANING.get(grid_basis, 'unrecognised')}. "
                "Basis matters as much as the value: location-based and market-based "
                "factors are not interchangeable and must never be summed. A regional or "
                "supplier factor is strictly better than any global average — eGRID "
                "subregions span more than 10x. "
            )
            + GRID_PRECEDENCE_NOTE
            + " "
            + GRID_NO_INFERENCE_NOTE,
            # The one setting that actually applied — not a list of candidates.
            # None for a factor handed to the accounting call: there is no setting
            # to change. The precedence order is in the note above.
            grid_setting,
            basis=grid_basis,
            overridden=bool(grid_overridden),
            # Which rule chose this factor, as a stable key, plus the operator's own
            # label for it. `source` above is a citation string for humans; these two
            # are what a UI groups and explains by.
            source_key=grid_source,
            source_rule=grid_source_rule(grid_source),
            source_label=grid_source_label,
            layer=factors.grid.layer,
            # Phase 3, additive: an operator-pinned region (see
            # tret.services.grid_regions), and whether this value came from
            # an hourly table or the plain annual figure (see
            # tret.services.grid_tables). All None/"annual_average"/False
            # when neither feature is in play — exactly today's shape.
            grid_region=factors.grid.region,
            requested_region=factors.grid.requested_region,
            region_resolution_status=factors.grid.region_resolution_status,
            temporal=factors.grid.temporal,
            table=factors.grid.table,
            table_summary=factors.grid.table_summary,
            table_miss=factors.grid.table_miss,
            factor_boundary=factors.grid.factor_boundary,
            gas_coverage=factors.grid.gas_coverage,
            gwp_horizon_years=factors.grid.gwp_horizon_years,
            gwp_assessment_basis=factors.grid.gwp_assessment_basis,
            includes_td_losses=factors.grid.includes_td_losses,
            electricity_mix_basis=factors.grid.electricity_mix_basis,
            dataset_version=factors.grid.dataset_version,
            observation_year=factors.grid.observation_year,
        ),
        _factor(
            "embodied_hardware",
            "Amortized embodied (manufacturing) carbon",
            _f(embodied_g),
            "gCO2e per run",
            EMBODIED_REFERENCE["source"],
            EMBODIED_REFERENCE["url"],
            EMBODIED_REFERENCE["date"],
            "placeholder",
            (
                (
                    "Opt-in and 0 by default, which understates self-hosted inference. "
                    if deployment == DEPLOYMENT_LOCAL
                    else "Not separately estimated for cloud: it is inside the purchased service. "
                )
                + "Reference constants: NVIDIA H100 273 kgCO2eq/unit, server chassis "
                "excluding GPUs 5,700 kgCO2eq, over a 3-year lifetime at batch size 64. "
                + EMBODIED_REFERENCE["caveat"]
                + (
                    " This run's figure was computed from a named hardware profile "
                    "(gpus/lifetime/batch size below), resting on the same placeholder "
                    "constants above — a profile changes the inputs, not the "
                    "confidence of the constants they feed."
                    if factors.embodied_g.profile is not None
                    else ""
                )
            ),
            factors.embodied_g.setting,
            gpu_h100_kg=EMBODIED_REFERENCE["gpu_h100_kg"],
            server_excluding_gpus_kg=EMBODIED_REFERENCE["server_excluding_gpus_kg"],
            lifetime_years=EMBODIED_REFERENCE["lifetime_years"],
            batch_size=EMBODIED_REFERENCE["batch_size"],
            layer=factors.embodied_g.layer,
            profile=factors.embodied_g.profile,
        ),
        _factor(
            "training_amortization",
            "Amortized training emissions",
            0.0,
            "gCO2e per run",
            "excluded by design — see note",
            None,
            None,
            "excluded",
            TRAINING_AMORTIZATION_EXCLUDED,
            layer=LAYER_GLOBAL_DEFAULT,
        ),
        _factor(
            "token_prices",
            "Per-token list prices",
            None,
            "USD per million tokens",
            "provider published price lists, via providers/models.yaml",
            None,
            None,
            "exact",
            (
                "The only exact inputs here. Prices are published per token, so the "
                "dollar figures are arithmetic; the *counterfactual* they are compared "
                "against is still an assumption."
            ),
            "models.yaml: input_price_per_mtok / output_price_per_mtok",
            layer=LAYER_GLOBAL_DEFAULT,
        ),
        _factor(
            "uncertainty_band",
            "Uncertainty band factor",
            [_f(low, 3), _f(high, 3)],
            "multiplicative, [low divisor, high multiplier]",
            "; ".join(f"{s['name']}: {s['claim']}" for s in UNCERTAINTY_SOURCES),
            UNCERTAINTY_SOURCES[0]["url"],
            "2025",
            "low",
            (
                "A judgment band matching field practice, NOT a confidence interval "
                "and not a sigma. Nothing in this field publishes an interval."
            ),
            "TRET_UNCERTAINTY_BAND_LOW / TRET_UNCERTAINTY_BAND_HIGH",
            layer=band_layer,
            derived=bool(factors.band_low.derived or factors.band_high.derived),
        ),
    ]
    return factors


def caveat_records(
    *,
    reasoning_tier: bool,
    deployment: str,
    cost_usd: Decimal | None = None,
    grid_basis: str | None = None,
    baseline_grid_basis: str | None = None,
    baseline_grid_compatible: bool | None = None,
    active_params_unknown: bool = False,
    active_params_gpu_only: bool = False,
    measured: bool = False,
    energy_strategy: str | None = None,
    embodied_double_add_prevented: bool = False,
) -> list[dict]:
    """Named biases that travel with the figures instead of living only in a doc.

    Every one of these is a known way the number is wrong. They are structured
    rather than prose so a UI can surface the ones that apply to a given run.

    `energy_strategy` (the run's `method_id`, e.g. `class_ladder_v1` /
    `class_ladder_v2`) drives `legacy_source_pue_double_count`: `applies=True`
    only for a `class_ladder_v1` rollback run, and `applies=False` but still
    listed for a `class_ladder_v2` run, the same "considered, does not apply
    here" treatment `measured=True` gives `prompt_shape_residual` below — see
    the `flipped` handling at the end of this function. `active_params_unknown=True`
    adds a caveat naming that the configured
    `"active_params"` strategy fell back to the class ladder because this
    model has no `active_params_b`. `active_params_gpu_only=True` (the
    active-parameter formula actually priced this run) adds the companion
    caveat naming the GPU-count and server-energy terms EcoLogits' own
    pipeline includes and tret's does not — see
    `_active_params_energy_constant`. `measured=True` (an operator-supplied
    `measured_energy_wh`) flips `unbatched_local_inference` and
    `prompt_shape_residual` to `applies: False` — both concerns are about the
    gap between a per-token model and the real thing, which a direct
    measurement closes — and keeps them in the list rather than dropping them,
    so a reader can see they were considered and why they do not apply here.
    """
    out = [
        {
            "key": "reasoning_token_accounting",
            "label": "Reasoning tokens may not be in the counted output",
            "direction": "understates",
            "applies": True,
            "note": (
                "tret's token counts come from provider usage reporting. For several "
                "providers hidden reasoning tokens are not included in the billed "
                "output count, so real generation work — and therefore energy — is "
                "higher than counted, and energy per *visible* output token is "
                "inflated for those models. This is a real bias, named rather than "
                "silently absorbed."
                + (
                    " This run used a reasoning-tier model, so it applies directly."
                    if reasoning_tier
                    else ""
                )
            ),
        },
        {
            "key": "prompt_shape_residual",
            "label": "Per-token energy still drifts with prompt shape",
            "direction": "either",
            "applies": True,
            "note": (
                "In the reference data, flat per-token energy falls ~4-6x from the "
                "short to the long prompt shape (Claude 3.7 Sonnet: 2,100 -> 480 "
                "Wh/Mtok). Weighting input and output separately removes most of that "
                "artifact — the same figures on an output-equivalent basis vary only "
                "1.04x (Claude), 1.18x (o3), 1.46-1.54x (nano, GPT-4o). DeepSeek-R1 is "
                "the exception at 4.64x: its short-prompt figure is anomalously high, "
                "which is also why its fit is degenerate. A linear model cannot "
                "express that residual and tret does not pretend to."
            ),
        },
        {
            "key": "same_token_counterfactual",
            "label": "Baseline comparisons hold tokens fixed",
            "direction": "either",
            "applies": True,
            "note": (
                "Both avoided_co2e_g and avoided_usd re-price *these* token counts "
                "through another model. A different model would not have produced "
                "them — it might need more turns, or produce a worse answer someone "
                "redoes. Efficiency indicator, not a saving."
            ),
        },
        {
            "key": "cloud_embodied_excluded",
            "label": "Cloud manufacturing carbon is not counted",
            "direction": "understates",
            "applies": deployment != DEPLOYMENT_LOCAL,
            "note": (
                "Embodied hardware for cloud inference sits inside the purchased "
                "service (Cat. 1) and tret has no basis for splitting it out, so a "
                "cloud run's Scope 3 is electricity-derived only."
            ),
        },
        {
            "key": "unbatched_local_inference",
            "label": "Local inference is not batched",
            "direction": "understates",
            "applies": deployment == DEPLOYMENT_LOCAL,
            "note": (
                "The class constants come from batched serving stacks (the reference "
                "data infers batch sizes in the dozens). A single-user local model "
                "carries the whole accelerator for one request, so its real per-token "
                "energy can be several times class S."
            ),
        },
        {
            "key": "training_excluded",
            "label": "Training emissions are not allocated",
            "direction": "understates",
            "applies": True,
            "note": TRAINING_AMORTIZATION_EXCLUDED,
        },
        {
            "key": "money_excludes_self_hosting_costs",
            "label": "Money comparison excludes self-hosting costs",
            # The reported saving is more flattering than the real one: the true
            # cost of running this model is understated in dollars, so the
            # percentage cheaper than frontier overstates how cheap it really is.
            "direction": "overstates",
            "applies": deployment == DEPLOYMENT_LOCAL and (cost_usd or Decimal(0)) <= 0,
            "note": (
                "The money comparison is list-price API spend only — published "
                "per-token prices, nothing else. A self-hosted run's zero dollar "
                "cost excludes the electricity and hardware amortization that "
                "actually running it costs, so a figure such as \"100% cheaper "
                "than frontier\" is true of billed API spend only, not of total "
                "cost. This is a deliberate asymmetry with the carbon accounting "
                "above, which DOES attribute Scope 2 electricity (and, if "
                "TRET_EMBODIED_G_PER_RUN is set, embodied hardware) to this same "
                "run — so a run that reads 100% cheaper here can still carry a "
                "real, nonzero carbon figure. Money tracks what tret's token API "
                "bills; carbon tracks what running the model actually draws."
            ),
        },
        {
            "key": "baseline_crosses_grid_basis",
            "label": "The baseline comparison spans incompatible grid factors",
            "direction": "either",
            "applies": (
                not baseline_grid_compatible
                if baseline_grid_compatible is not None
                else bool(grid_basis and baseline_grid_basis and grid_basis != baseline_grid_basis)
            ),
            "note": (
                f"This run's electricity is accounted {grid_basis}; the baseline "
                f"counterfactual was priced {baseline_grid_basis}, because the two "
                "providers carry different configured factors. Under the GHG Protocol "
                "those figures answer different questions and may not be summed or "
                "netted, so avoided_co2e_g here is a model-selection signal only and is "
                "not a difference between two comparable inventories. Configure one "
                "basis across your providers if you need the comparison to be like for "
                "like."
            ),
        },
        {
            "key": "out_of_scope_energy",
            "label": "Only the execution model's turns are counted",
            "direction": "understates",
            "applies": True,
            "note": (
                "Excluded: water, network transfer, storage, retrieval and embedding "
                "calls, and the router's own model call — the same scope as the dollar "
                "cost tret already reports."
            ),
        },
        {
            "key": "active_params_unknown",
            "label": "Active-parameter strategy fell back to the class ladder",
            "direction": "either",
            "applies": active_params_unknown,
            "note": (
                "The configured energy strategy asked for EcoLogits' "
                "active-parameter formula, but the catalog has no "
                "active_params_b for this model, so this run priced tokens on "
                "the class ladder instead — the same fallback "
                "wh_per_mtok_for_model has always used when no better estimate "
                "is available."
            ),
        },
        {
            "key": "active_params_gpu_only",
            "label": "Active-parameter formula is per-GPU, GPU-energy only",
            "direction": "understates",
            "applies": active_params_gpu_only,
            "note": (
                "This run's energy came from EcoLogits' active-parameter formula, "
                "which models GPU energy for a single accelerator only. EcoLogits' "
                "own pipeline multiplies that per-GPU figure by the GPU count the "
                "model needs and adds a separate server/host energy term before "
                "comparing it to a whole-request figure; tret has neither the GPU "
                "count nor the server-energy term, so this run's energy is priced "
                "as if it were both — a systematic undercount, not a rounding "
                "error. This is why the active-parameter strategy records "
                "confidence 'low' rather than 'calibrated'."
            ),
        },
        {
            "key": "legacy_source_pue_double_count",
            "label": "Rollback constants double count facility overhead",
            "direction": "overstates",
            "applies": energy_strategy == "class_ladder_v1",
            "note": (
                "class_ladder_v1 constants were fitted to source observations that "
                "already include the provider's PUE (Jegham 2025 Eq. 1); deployment "
                "PUE is applied again on this run. Retained for rollback fidelity; "
                "use class_ladder_v2 for the corrected boundary."
            ),
        },
        {
            "key": "embodied_double_add_prevented",
            "label": "A duplicate embodied figure was dropped",
            "direction": "either",
            "applies": embodied_double_add_prevented,
            "note": (
                "This run configured `supplier_includes_inference_hardware` together "
                "with a separately supplied embodied figure (a flat g_per_run or a "
                "time-based allocation). Summing both would double-count "
                "manufacturing carbon that the supplier total already carries, so "
                "the second figure was dropped rather than added — this run's "
                "embodied_g reflects the supplier total only."
            ),
        },
    ]
    flipped: set[str] = set()
    if energy_strategy == "class_ladder_v2":
        # Not true of this run, but named anyway so a reader can see it was
        # considered — the same "listed, does not apply" treatment `measured`
        # gives the two caveats below.
        flipped.add("legacy_source_pue_double_count")
    if measured:
        for c in out:
            if c["key"] == "prompt_shape_residual":
                c["applies"] = False
                flipped.add(c["key"])
                c["note"] += (
                    " This run's energy was operator-measured (IT-load Wh) "
                    "directly rather than modeled per token, so this residual "
                    "does not apply here."
                )
            elif c["key"] == "unbatched_local_inference" and deployment == DEPLOYMENT_LOCAL:
                c["applies"] = False
                flipped.add(c["key"])
                c["note"] += (
                    " This run's energy was operator-measured directly, so it "
                    "already reflects the real batching (or lack of it) at "
                    "serving time rather than the class constants' assumed "
                    "batch size."
                )
    return [c for c in out if c["applies"] or c["key"] in flipped]


# ── frontier baseline ────────────────────────────────────────────────────────
def resolve_baseline_model(
    settings: Settings | None = None, catalog: ModelCatalog | None = None
) -> ModelInfo | None:
    """The counterfactual comparison model, or None if there isn't one.

    `emissions_baseline_model` names it explicitly. Empty (the default) picks the
    highest-energy-class curated non-local model, tie-broken by model id so the
    choice is deterministic across processes. A configured id the catalog does
    not know resolves to None rather than silently falling back to a different
    model: a quiet substitution would make the number unauditable.
    """
    settings = settings or get_settings()
    if catalog is None:
        from tret.providers.catalog import get_catalog

        catalog = get_catalog()
    configured = (settings.emissions_baseline_model or "").strip()
    if configured:
        return catalog.get(configured)
    candidates = [m for m in catalog.all(curated_only=True) if m.provider != LOCAL_PROVIDER]
    if not candidates:
        return None
    return sorted(candidates, key=lambda m: (-(m.energy_wh_per_mtok or Decimal(0)), m.id))[0]


def _no_baseline() -> dict:
    return {
        "model": None,
        "energy_class": None,
        "energy_wh": None,
        "energy_wh_total": None,
        "co2e_g": None,
        "avoided_co2e_g": None,
        "avoided_pct": None,
        # Added with money-saved; null for the same reason as the carbon fields.
        "cost_usd": None,
        "avoided_usd": None,
        "avoided_usd_pct": None,
        # No comparison, so no factor behind one either. Null, not the run's own.
        "grid_co2e_g_per_kwh": None,
        "grid_co2e_basis": None,
        "grid_co2e_source": None,
        "basis": _NO_BASELINE_BASIS,
    }


def _baseline_block(
    model: ModelInfo,
    tokens: tuple[int, int, int, int],
    actual_co2e_g: Decimal,
    actual_energy_wh: Decimal,
    actual_energy_wh_total: Decimal,
    actual_cost_usd: Decimal,
    grid_g_per_kwh: float | None,
    settings: Settings,
    catalog: ModelCatalog | None,
    actual_factors: "FactorSet",
    actual_factor_records: list[dict],
    energy_tokens: tuple[int, int, int, int] | None = None,
) -> dict:
    baseline = resolve_baseline_model(settings, catalog)
    if baseline is None:
        return _no_baseline()
    # The counterfactual is "these tokens through *that* model", so it is priced at
    # the factor that model's provider would have carried — including a
    # TRET_GRID_FACTORS entry of its own. That is honest per-side, and it means the
    # two sides of the comparison can sit on different GHG Protocol bases; when they
    # do, the run carries the `baseline_crosses_grid_basis` caveat saying so.
    from tret.services.emission_factors import (
        LAYER_GLOBAL_DEFAULT,
        build_factor_set,
        factor_set_for_model,
    )

    baseline_deployment = deployment_for(baseline.provider)
    baseline_factors = factor_set_for_model(
        actual_factors, provider=baseline.provider, model_id=baseline.id
    )
    if baseline_factors is None:
        # Legacy/manual FactorSets did not retain their resolution inputs.  Fall
        # back to Settings rather than reusing provider-specific actual values.
        baseline_factors = build_factor_set(
            provider=baseline.provider,
            settings=settings,
            run_overrides=(
                {"grid_g_per_kwh": grid_g_per_kwh}
                if grid_g_per_kwh is not None
                else None
            ),
            model_id=baseline.id,
        )
    baseline_grid = baseline_factors.grid
    baseline_constant, _ = _energy_constant_and_flags(baseline, baseline_factors)
    if baseline_factors.model_override is not None:
        baseline_energy_layer = baseline_factors.model_override.layer
        baseline_energy_setting = baseline_factors.model_override.setting
    elif baseline_constant.strategy == "active_params":
        baseline_energy_layer = baseline_factors.energy_strategy.layer
        baseline_energy_setting = baseline_factors.energy_strategy.setting
    else:
        baseline_energy_layer = LAYER_GLOBAL_DEFAULT
        baseline_energy_setting = "models.yaml: energy_class / energy_wh_per_mtok"
    baseline_provenance = factor_records(
        energy_class=baseline.energy_class,
        deployment=baseline_deployment,
        settings=settings,
        factors=baseline_factors,
        energy_constant=baseline_constant,
        energy_layer=baseline_energy_layer,
        energy_setting=baseline_energy_setting,
    )
    if baseline.id == model.id:
        # The run *is* the baseline. Avoided is 0 by construction, not by
        # arithmetic: comparing a run to itself has no counterfactual in it.
        return {
            "model": baseline.id,
            "energy_class": baseline.energy_class,
            "energy_wh": _f(actual_energy_wh),
            "energy_wh_total": _f(actual_energy_wh_total),
            "co2e_g": _f(actual_co2e_g),
            "avoided_co2e_g": 0.0,
            "avoided_pct": 0.0,
            "cost_usd": _f(actual_cost_usd),
            "avoided_usd": 0.0,
            "avoided_usd_pct": 0.0,
            "grid_co2e_g_per_kwh": float(actual_factors.grid.value),
            "grid_co2e_basis": actual_factors.grid_basis,
            "grid_co2e_source": actual_factors.grid.source,
            "grid_region": actual_factors.grid.region,
            "grid_temporal": actual_factors.grid.temporal or "annual_average",
            "grid_factor_boundary": actual_factors.grid.factor_boundary,
            "grid_gas_coverage": actual_factors.grid.gas_coverage,
            "grid_gwp_horizon_years": actual_factors.grid.gwp_horizon_years,
            "grid_gwp_assessment_basis": actual_factors.grid.gwp_assessment_basis,
            "grid_includes_td_losses": actual_factors.grid.includes_td_losses,
            "grid_electricity_mix_basis": actual_factors.grid.electricity_mix_basis,
            "factors": actual_factor_records,
            "basis": (
                _BASELINE_BASIS + " This run used the baseline model itself, so avoided is 0."
            ),
        }
    pue = baseline_factors.pue.value
    grid = baseline_grid.value
    compute_wh = sum(
        energy_wh_by_bucket(baseline_constant.wh_per_mtok, *(energy_tokens or tokens)).values(), Decimal(0)
    )
    total_wh = compute_wh * pue
    electricity_g = round(co2e_grams(total_wh, grid), _PLACES)
    embodied_g = round(baseline_factors.embodied_g.value, _PLACES)
    baseline_co2e = electricity_g + embodied_g
    def _signature(grid_factor, grid_basis):
        return grid_comparison_signature({
            "grid_co2e_g_per_kwh": float(grid_factor.value),
            "grid_co2e_basis": grid_basis,
            "grid_co2e_source": grid_factor.source,
            "grid_co2e_label": grid_factor.label,
            "grid_co2e_layer": grid_factor.layer,
            "grid_factor_boundary": grid_factor.factor_boundary,
            "grid_gas_coverage": grid_factor.gas_coverage,
            "grid_gwp_horizon_years": grid_factor.gwp_horizon_years,
            "grid_gwp_assessment_basis": grid_factor.gwp_assessment_basis,
            "grid_includes_td_losses": grid_factor.includes_td_losses,
            "grid_electricity_mix_basis": grid_factor.electricity_mix_basis,
        })

    actual_signature = _signature(actual_factors.grid, actual_factors.grid_basis)
    baseline_signature = _signature(baseline_grid, baseline_factors.grid_basis)
    carbon_compatible = actual_signature == baseline_signature
    avoided = (
        baseline_co2e - round(actual_co2e_g, _PLACES) if carbon_compatible else None
    )
    avoided_pct = (
        _f(Decimal(100) * avoided / baseline_co2e, 3)
        if avoided is not None and baseline_co2e > 0
        else 0.0 if avoided is not None else None
    )
    baseline_cost = round(baseline.cost_usd(*tokens), _PLACES)
    avoided_usd = baseline_cost - round(actual_cost_usd, _PLACES)
    # Null, not 0.0, when the baseline itself costs nothing: a percentage needs a
    # nonzero denominator, and a misconfigured baseline pointed at a free model
    # must not render as "0% cheaper" (which would read as "no difference").
    avoided_usd_pct = (
        _f(Decimal(100) * avoided_usd / baseline_cost, 3) if baseline_cost > 0 else None
    )
    return {
        "model": baseline.id,
        "energy_class": baseline.energy_class,
        "energy_wh": _f(compute_wh),
        "energy_wh_total": _f(total_wh),
        "co2e_g": _f(baseline_co2e),
        # Signed on purpose: a run that used something heavier than the baseline
        # avoided nothing, and clamping that to zero would be a greenwash.
        "avoided_co2e_g": _f(avoided) if avoided is not None else None,
        "avoided_pct": avoided_pct,
        # Money, on exactly the same signed same-token basis. Negative means this
        # run cost *more* than the baseline would have — a surcharge, not a saving.
        "cost_usd": _f(baseline_cost),
        "avoided_usd": _f(avoided_usd),
        "avoided_usd_pct": avoided_usd_pct,
        # The factor the counterfactual side was priced at, and its basis. Recorded
        # because a comparison across two bases is not a GHG Protocol total, and a
        # reader has to be able to see that from the stored run.
        "grid_co2e_g_per_kwh": float(grid),
        "grid_co2e_basis": baseline_factors.grid_basis,
        "grid_co2e_source": baseline_grid.source,
        "grid_region": baseline_grid.region,
        "grid_temporal": baseline_grid.temporal or "annual_average",
        "grid_factor_boundary": baseline_grid.factor_boundary,
        "grid_gas_coverage": baseline_grid.gas_coverage,
        "grid_gwp_horizon_years": baseline_grid.gwp_horizon_years,
        "grid_gwp_assessment_basis": baseline_grid.gwp_assessment_basis,
        "grid_includes_td_losses": baseline_grid.includes_td_losses,
        "grid_electricity_mix_basis": baseline_grid.electricity_mix_basis,
        "carbon_compatible_with_actual": carbon_compatible,
        "factors": baseline_provenance,
        "basis": _BASELINE_BASIS,
    }


def _cost_block(actual_usd: Decimal, baseline: dict) -> dict:
    """Money saved (or overspent) against the same-token baseline.

    The most defensible number in this module, and worth saying why: per-token
    prices are published and exact, so `usd` and `baseline_usd` are arithmetic,
    not estimation. Only the counterfactual is assumed — and it is the same
    assumption the carbon comparison already makes, so the two travel together
    and are signed the same way.
    """
    return {
        "usd": _f(actual_usd),
        "baseline_model": baseline.get("model"),
        "baseline_usd": baseline.get("cost_usd"),
        "avoided_usd": baseline.get("avoided_usd"),
        "avoided_pct": baseline.get("avoided_usd_pct"),
        # True of the prices, not of the comparison. Named narrowly on purpose.
        "prices_are_exact": True,
        "basis": _COST_BASIS,
    }


# ── the persisted per-run block ──────────────────────────────────────────────
def scope_split(deployment: str, electricity_g: Decimal, embodied_g: Decimal) -> dict:
    """{scope1_g, scope2_g, scope3_g, basis}, all rounded so they sum exactly.

    Scope 1 is always 0.0 and always present. Cloud electricity is Scope 3
    (purchased service); self-hosted electricity is Scope 2 (purchased power)
    and self-hosted embodied hardware is Scope 3 (capital goods).
    """
    electricity = round(electricity_g, _PLACES)
    embodied = round(embodied_g, _PLACES)
    if deployment == DEPLOYMENT_LOCAL:
        scope2, scope3 = electricity, embodied
    else:
        scope2, scope3 = Decimal(0), electricity + embodied
    return {
        "scope1_g": 0.0,
        "scope2_g": _f(scope2),
        "scope3_g": _f(scope3),
        "basis": _SCOPE_BASIS,
    }


def energy_accounting(
    model: ModelInfo,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    grid_g_per_kwh: float | None = None,
    *,
    settings: Settings | None = None,
    catalog: ModelCatalog | None = None,
    factors: "FactorSet | None" = None,
    measured_energy_wh: float | None = None,
    measured_energy_boundary: str = "node_it",
    validated_evidence: "Evidence | None" = None,
    embodied_allocation: dict | None = None,
    supplier_includes_inference_hardware: bool = False,
    reasoning_tokens: int | None = None,
    reasoning_accounting: str = "unknown",
    energy_output_tokens: int | None = None,
) -> dict:
    """The auditable energy/carbon breakdown persisted on a run.

    Every field is an estimate derived from token counts, the model's calibrated
    energy class, a per-deployment PUE and a grid intensity — nothing here is
    metered, *unless* `measured_energy_wh` is given (see below). JSON-serializable
    throughout (floats, strings, lists; no Decimals).

    Key meanings, unchanged from the original contract:

    * `energy_wh` — **compute (IT-load) energy only**, no data-centre overhead.
    * `energy_wh_per_mtok` — Wh per million tokens, now per million
      *output-equivalent* tokens (see `weighted_tokens`); the chain
      `energy_wh = energy_wh_per_mtok x weighted_tokens / 1e6` is unchanged
      whenever `measured_energy_wh` is not given.
    * `grid_co2e_g_per_kwh` — the factor actually applied to this run's
      electricity: the operator's per-provider entry when there is one, else the
      legacy local override on a self-hosted run, else the global default.
      `grid_co2e_source` records which of those applied and `grid_co2e_label`
      carries the operator's own note about it.
    * `co2e_g` — the run's **total** estimated carbon, and always exactly
      `scopes.scope1_g + scope2_g + scope3_g`.
    * `pue`, `energy_wh_total`, `deployment`, `embodied_g`, `scopes`,
      `baseline` — as before.

    Added, all additive: `input_weight`, `output_weight`,
    `energy_wh_per_mtok_input`, `tokens`, `energy_wh_by_bucket`,
    `reasoning_tier`, `pue_profile`, `grid_co2e_basis`, `grid_co2e_source`,
    `grid_co2e_label`, `grid_co2e_layer`, `cost`, `uncertainty`, `factors`,
    `factor_layers`, `caveats`, `energy_wh_estimated`, `energy_source`.

    `factors` — a `tret.services.emission_factors.FactorSet` — is the layered
    resolution (`run_override > harness > workspace > managed > env >
    dataset > global_default`) of every constant below. Left `None` (every existing
    caller), one is built here from `settings` alone (and `model.id`, so a
    `model_overrides` layer applies automatically), plus `grid_g_per_kwh` as a
    run override exactly as it always has been — so this call is byte-for-byte
    what it was before `FactorSet` existed. Given one, its values are used
    instead; `grid_g_per_kwh`, if *also* passed, still wins as a run override
    on top of it, same as it would on top of `settings` alone.

    `measured_energy_wh` — a measured figure (Wh) at the explicitly supplied
    `measured_energy_boundary` (default `node_it` for compatibility). Must be `>= 0`; a negative value
    raises `ValueError`. When given, it — not the per-token estimate — becomes
    `energy_wh`; the estimate the model/strategy would otherwise have produced
    is kept as `energy_wh_estimated` (present on every run, measured or not,
    and equal to `energy_wh` when there is no measurement), and
    `energy_wh_by_bucket` is scaled proportionally from the estimate so the
    buckets still sum to the measured `energy_wh`. PUE, grid intensity and
    embodied hardware still apply **on top** of the measurement exactly as
    they would on top of an estimate — the measurement is IT-load only, not a
    substitute for hardware amortization. PUE is not applied to facility,
    partial, or unknown-boundary readings. See
    docs/emissions-methodology.md's "Energy strategies and measured runs".
    """
    settings = settings or get_settings()
    deployment = deployment_for(model.provider)

    allowed_boundaries = {"gpu", "node_it", "facility", "partial", "unknown"}
    if measured_energy_boundary not in allowed_boundaries:
        raise ValueError(f"unsupported measured_energy_boundary: {measured_energy_boundary!r}")
    if measured_energy_wh is not None and (
        not math.isfinite(measured_energy_wh) or measured_energy_wh < 0
    ):
        raise ValueError(
            f"measured_energy_wh must be finite and >= 0, got {measured_energy_wh!r}"
        )
    if reasoning_tokens is not None and reasoning_tokens < 0:
        raise ValueError("reasoning_tokens must be nonnegative")
    if reasoning_accounting == "included_in_output":
        reasoning_accounting = "counted_in_output"
    if reasoning_accounting not in {"counted_in_output", "additional", "unknown", None}:
        raise ValueError("unsupported reasoning_accounting")
    reasoning_accounting = reasoning_accounting or "unknown"
    if energy_output_tokens is not None and (
        isinstance(energy_output_tokens, bool) or not isinstance(energy_output_tokens, int)
        or energy_output_tokens < output_tokens
    ):
        raise ValueError("energy_output_tokens must be an integer at least billed output_tokens")

    # Lazy: `emission_factors` imports plain functions from this module at its
    # own top level, so importing it back at *this* module's top level would
    # be circular. See the identical note on `factor_records`.
    from tret.services.emission_factors import (
        LAYER_GLOBAL_DEFAULT,
        LAYER_RUN_OVERRIDE,
        Resolved,
        build_factor_set,
        context_with_run_overrides,
    )

    if factors is None:
        run_overrides = (
            {"grid_g_per_kwh": grid_g_per_kwh} if grid_g_per_kwh is not None else None
        )
        factors = build_factor_set(
            provider=model.provider, settings=settings, run_overrides=run_overrides,
            model_id=model.id,
        )
    elif grid_g_per_kwh is not None:
        # `factors` was handed to us already built, but this call *also* got an
        # explicit `grid_g_per_kwh` — that still outranks whatever `factors`
        # resolved, exactly as it would have on top of `settings` alone.
        context = factors.resolution_context
        if context is not None:
            context = context_with_run_overrides(
                context, {"grid_g_per_kwh": grid_g_per_kwh}
            )
        factors = dataclasses.replace(
            factors,
            grid=Resolved(
                Decimal(str(grid_g_per_kwh)), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                None, None, None, None,
            ),
            grid_basis=GRID_BASIS_UNSPECIFIED,
            layers_present=tuple(
                dict.fromkeys((LAYER_RUN_OVERRIDE, *factors.layers_present))
            ),
            resolution_context=context,
        )

    pue = factors.pue.value
    profile = factors.pue_profile
    grid = float(factors.grid.value)
    grid_basis = factors.grid_basis

    tokens = (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    energy_tokens = (input_tokens, energy_output_tokens if energy_output_tokens is not None else output_tokens,
                     cache_read_tokens, cache_write_tokens)
    energy_constant, active_params_unknown = _energy_constant_and_flags(model, factors)
    # The only path that ever sets this strategy is `_active_params_energy_constant`
    # — a `model_overrides` win's strategy mirrors its own confidence label, never
    # this string. See that function's docstring for the GPU-only/no-server-energy
    # scope gap this caveat names.
    active_params_gpu_only = energy_constant.strategy == "active_params"
    wh_per_mtok = energy_constant.wh_per_mtok
    energy_class = getattr(model, "energy_class", DEFAULT_ENERGY_CLASS)
    reasoning_tier = is_reasoning_class(energy_class)

    # Which layer *decided this run's energy constant* — for the "energy_class"
    # provenance record. A `model_overrides` win carries its own layer/setting;
    # an `active_params` win carries the layer that configured the strategy
    # (the formula itself is not a layered document); the plain class ladder
    # has never been layered, so it is always `global_default`.
    if factors.model_override is not None:
        energy_layer = factors.model_override.layer
        energy_setting = factors.model_override.setting
    elif energy_constant.strategy == "active_params":
        energy_layer = factors.energy_strategy.layer
        energy_setting = factors.energy_strategy.setting
    else:
        energy_layer = LAYER_GLOBAL_DEFAULT
        energy_setting = "models.yaml: energy_class / energy_wh_per_mtok"

    by_bucket_estimated = energy_wh_by_bucket(wh_per_mtok, *energy_tokens)
    compute_wh_estimated = sum(by_bucket_estimated.values(), Decimal(0))
    is_measured = measured_energy_wh is not None
    if is_measured:
        compute_wh = Decimal(str(measured_energy_wh))
        if compute_wh_estimated > 0:
            scale = compute_wh / compute_wh_estimated
            by_bucket = {k: v * scale for k, v in by_bucket_estimated.items()}
        else:
            # Nothing to scale from (e.g. zero weighted tokens) — put the whole
            # measurement in `output`, the dominant bucket, rather than divide
            # by zero. Buckets still sum to `energy_wh` either way.
            by_bucket = {
                k: (compute_wh if k == "output" else Decimal(0)) for k in by_bucket_estimated
            }
    else:
        compute_wh = compute_wh_estimated
        by_bucket = by_bucket_estimated

    energy_boundary = (
        measured_energy_boundary
        if is_measured
        else (
            "unknown" if factors.model_override is not None or getattr(model, "energy_wh_per_mtok_explicit", False)
            else
            "gpu" if energy_constant.strategy == "active_params"
            else "node_it" if energy_constant.strategy == "class_ladder_v2"
            else "unknown"
        )
    )
    # Legacy modeled coefficients keep their as-versioned arithmetic until A3.
    # Only measured boundaries can safely change the conversion in this version.
    pue_is_applied = not is_measured or energy_boundary in {"gpu", "node_it"}
    applied_pue = pue if pue_is_applied else Decimal(1)
    total_wh = compute_wh * applied_pue
    electricity_g = co2e_grams(total_wh, grid)
    embodied_g = factors.embodied_g.value
    # Accounting must never fail a run over embodied hardware. A supplier
    # total that already bundles inference hardware, combined with a second,
    # separately-configured embodied figure (a flat g_per_run or a time
    # allocation), would double-count manufacturing carbon if summed — so the
    # second figure is dropped, not summed, and named with a caveat rather
    # than raised. Strict validation for an operator *document* (rejecting a
    # profile that mixes a supplier-inclusive total with its own allocation
    # at configuration time) still lives in `embodied_profiles.py`; this is
    # the accounting path, which degrades instead of failing.
    embodied_double_add_prevented = bool(
        supplier_includes_inference_hardware and (embodied_g > 0 or embodied_allocation is not None)
    )
    embodied_known = True
    if embodied_double_add_prevented:
        embodied_g = Decimal(0)
        embodied_allocation = None
        logger.warning(
            "energy_accounting: supplier_includes_inference_hardware is set together "
            "with a separately configured embodied figure for %s; dropping the "
            "embodied figure instead of double-counting.",
            getattr(model, "id", "<unknown model>"),
        )
    elif embodied_allocation is not None:
        allocation_total = embodied_allocation.get("complete_total_g")
        if allocation_total is None:
            # Partial coverage: at least one hardware component in the
            # allocation has no known figure (see `allocate_by_time`).
            # Unknown, never zero, and never a reason to fail the run — the
            # allocation's own per-component detail (still attached below as
            # `result["embodied_allocation"]`) and the embodied factor record
            # carry what *is* known.
            embodied_known = False
            embodied_g = Decimal(0)  # arithmetic only; the output field is null below
        elif not math.isfinite(float(allocation_total)) or float(allocation_total) < 0:
            raise ValueError("embodied_allocation.complete_total_g must be finite and nonnegative")
        else:
            embodied_g = Decimal(str(allocation_total))
    cost_usd = model.cost_usd(*tokens)

    scopes = scope_split(deployment, electricity_g, embodied_g)
    # Total is the sum of the *rounded* scopes, so the invariant
    # co2e_g == scope1 + scope2 + scope3 holds exactly rather than nearly.
    total_g = (
        Decimal(str(scopes["scope1_g"]))
        + Decimal(str(scopes["scope2_g"]))
        + Decimal(str(scopes["scope3_g"]))
    )

    # The baseline counterfactual picks its own model from
    # `emissions_baseline_model` (via `resolve_baseline_model`); route the
    # layered value through by handing it a settings copy with that one field
    # swapped, rather than teaching `_baseline_block` a second way to learn it.
    baseline_settings = settings.model_copy(
        update={"emissions_baseline_model": factors.baseline_model.value}
    )
    actual_factor_records = factor_records(
        energy_class=energy_class,
        deployment=deployment,
        settings=settings,
        factors=factors,
        energy_constant=energy_constant,
        energy_layer=energy_layer,
        energy_setting=energy_setting,
        measured_energy=is_measured,
    )
    if is_measured:
        energy_factor = next(f for f in actual_factor_records if f["key"] == "energy_class")
        energy_factor["label"] = f"Measured energy ({measured_energy_boundary} boundary Wh)"
        energy_factor["source"] = f"Measured energy ({measured_energy_boundary} boundary Wh)"
        energy_factor["note"] = energy_factor["note"].replace(
            "IT-load Wh", f"{measured_energy_boundary}-boundary Wh"
        )
    if not pue_is_applied:
        pue_factor = next(f for f in actual_factor_records if f["key"] == "pue")
        pue_factor.update({
            "value": 1.0,
            "source": "Not applied at this measured energy boundary",
            "confidence": "exact",
            "note": (
                f"Configured PUE {float(pue)} was not multiplied into this {energy_boundary}-boundary "
                "measurement."
            ),
        })
    if embodied_allocation is not None and not embodied_known:
        embodied_factor = next(f for f in actual_factor_records if f["key"] == "embodied_hardware")
        covered = embodied_allocation.get("covered_subtotal_g")
        unknown_ids = [
            c.get("component_id")
            for c in embodied_allocation.get("components", [])
            if c.get("status") == "unknown"
        ]
        embodied_factor.update({
            "value": None,
            "confidence": "low",
            "note": (
                embodied_factor["note"]
                + f" This run's time allocation has a known covered subtotal of "
                f"{covered} gCO2e but is missing a figure for "
                + (", ".join(unknown_ids) if unknown_ids else "at least one component")
                + ", so the complete allocation is unknown rather than zero; "
                "embodied_g reports null and only the known electricity component "
                "is summed into co2e_g."
            ),
        })
        embodied_factor["embodied_allocation_covered_subtotal_g"] = covered
        embodied_factor["embodied_allocation_unknown_components"] = unknown_ids
    if embodied_double_add_prevented:
        embodied_factor = next(f for f in actual_factor_records if f["key"] == "embodied_hardware")
        embodied_factor.update({
            "value": 0.0,
            "confidence": "excluded",
            "note": (
                embodied_factor["note"]
                + " supplier_includes_inference_hardware was set together with a "
                "separately configured embodied figure on this run; the second "
                "figure was dropped rather than summed — see the "
                "embodied_double_add_prevented caveat."
            ),
        })
    baseline = _baseline_block(
        model,
        tokens,
        total_g,
        compute_wh,
        total_wh,
        cost_usd,
        grid_g_per_kwh,
        baseline_settings,
        catalog,
        factors,
        actual_factor_records,
        energy_tokens,
    )

    if is_measured:
        method_id = "measured_energy_v1"
        energy_source_by_component = {"reported_energy": "measured"}
    elif factors.model_override is not None:
        method_id = "model_override_v1"
        energy_source_by_component = {"unresolved_energy_boundary": "supplied"}
    elif getattr(model, "energy_wh_per_mtok_explicit", False):
        method_id = "catalog_override_v1"
        energy_source_by_component = {"unresolved_energy_boundary": "supplied"}
    elif energy_constant.strategy == "active_params":
        method_id = "active_params_v1"
        energy_source_by_component = {"gpu": "modeled"}
    else:
        method_id = (
            "class_ladder_v2"
            if energy_constant.strategy == "class_ladder_v2"
            else "class_ladder_v1"
        )
        energy_source_by_component = (
            {"gpu": "modeled", "non_gpu_it": "modeled"}
            if method_id == "class_ladder_v2"
            else {"unresolved_energy_boundary": "modeled"}
        )
    incomplete_boundary = energy_boundary in {"gpu", "partial", "unknown"}
    energy_method_shadow = None
    if not is_measured and method_id == "class_ladder_v2":
        legacy_constant = _class_ladder_energy_constant(model, "class_ladder_v1")
        legacy_compute = sum(
            energy_wh_by_bucket(legacy_constant.wh_per_mtok, *energy_tokens).values(), Decimal(0)
        )
        legacy_total = legacy_compute * pue
        legacy_co2e = co2e_grams(legacy_total, grid) + embodied_g
        energy_method_shadow = {
            "method_id": "class_ladder_v1",
            "calibration_id": "jegham_2025_legacy_unresolved_boundary",
            "energy_boundary": "unknown",
            "energy_wh": _f(legacy_compute),
            "energy_wh_total": _f(legacy_total),
            "co2e_g": _f(legacy_co2e),
            "difference_kind": "methodology_correction_not_emissions_savings",
        }

    # `included_components`/`excluded_components` describe the carbon *result*
    # (what is actually summed into co2e_g), not the calibration boundary —
    # see `energy_boundary_of_coefficients` below for that. Facility overhead
    # is either multiplied in via PUE (`pue_is_applied`) or, for a measurement
    # already taken at the facility boundary, already inside the measured
    # figure; either way it belongs in `included_components`, never in
    # `excluded_components`, or a consumer summing the two lists would double
    # it. A facility-inclusive but unnormalized constant (class_ladder_v1)
    # still gets PUE applied on top — see the `legacy_source_pue_double_count`
    # caveat, which names that specific double count rather than this list.
    facility_overhead_via_pue = pue_is_applied
    facility_overhead_measured = (not pue_is_applied) and energy_boundary == "facility"
    included_components = (
        (["gpu"] if energy_boundary == "gpu" else
         ["facility_energy"] if energy_boundary == "facility" else
         ["node_it_energy"] if energy_boundary == "node_it" else
         ["reported_partial_energy"] if energy_boundary == "partial" else [])
        + (["embodied_hardware"] if embodied_g > 0 else [])
        + (["facility_overhead_via_pue"] if facility_overhead_via_pue else [])
        + (["facility_overhead_measured"] if facility_overhead_measured else [])
    )
    excluded_components = (
        (["non_gpu_it"] if energy_boundary == "gpu" else [])
        + (
            [
                "unused_gpu_idle",
                # An unknown embodied allocation (embodied_known False) belongs
                # to the coverage record as unknown, not here — this list is
                # only for a component genuinely excluded (zero, not unknown).
                *(["embodied_hardware"] if embodied_g <= 0 and embodied_known else []),
            ]
            if method_id == "class_ladder_v2" else
            ["unmeasured_components"] if incomplete_boundary and energy_boundary != "gpu" else []
        )
    )

    result = {
        # ── original keys, original meanings ──
        "estimated": True,
        "method_id": method_id,
        "method_version": 2 if method_id == "class_ladder_v2" else 1,
        "calibration_id": (
            "jegham_2025_v2_node_it_fixed_weight"
            if method_id == "class_ladder_v2"
            else "jegham_2025_legacy_unresolved_boundary"
            if method_id == "class_ladder_v1"
            else None
        ),
        "energy_boundary": energy_boundary,
        # The calibration boundary the coefficients themselves were fit/
        # measured at — `node_it` for v2, `unknown` for v1 (facility-inclusive
        # but unnormalized), `gpu` for active_params, or the measured
        # boundary. Currently identical to `energy_boundary` above (which has
        # always meant the coefficient boundary, never the result boundary);
        # named separately so a consumer never has to guess which one a bare
        # `energy_boundary` key means.
        "energy_boundary_of_coefficients": energy_boundary,
        # `included_components`/`excluded_components` below describe the
        # result, not the coefficient boundary — see comment above.
        "component_lists_describe": "result",
        "included_components": included_components,
        "excluded_components": excluded_components,
        "energy_source_by_component": energy_source_by_component,
        "energy_boundary_complete": not incomplete_boundary,
        "energy_method_shadow": energy_method_shadow,
        "model": model.id,
        "energy_class": energy_class,
        "energy_wh_per_mtok": float(wh_per_mtok),
        "weighted_tokens": float(weighted_tokens(*energy_tokens)),
        "energy_output_tokens": energy_tokens[1],
        "cache_read_weight": float(ENERGY_CACHE_READ_MULTIPLIER),
        "cache_write_weight": float(ENERGY_CACHE_WRITE_MULTIPLIER),
        "energy_wh": _f(compute_wh),  # compute / IT load only — the measurement, if given
        # What the model/strategy would have produced from token counts alone.
        # Equal to `energy_wh` when this run is not measured; kept separately
        # when it is, so the estimate the buckets were scaled from is never lost.
        "energy_wh_estimated": _f(compute_wh_estimated),
        "energy_source": "measured" if is_measured else "estimated",
        "grid_co2e_g_per_kwh": float(grid),
        "co2e_g": _f(total_g),  # == scope1 + scope2 + scope3
        "basis": _ACCOUNTING_BASIS,
        "pue": float(applied_pue),
        "configured_pue": float(pue),
        "pue_applied": pue_is_applied,
        "energy_wh_total": _f(total_wh),  # compute x PUE
        "deployment": deployment,
        # Null, never 0, when an embodied_allocation had at least one
        # component with no known figure — a 0 here would claim the hardware
        # was priced at zero rather than never priced at all.
        "embodied_g": _f(embodied_g) if embodied_known else None,
        "scopes": scopes,
        "baseline": baseline,
        # ── added: the input/output split ──
        "input_weight": float(ENERGY_TOKEN_WEIGHT_INPUT),
        "output_weight": float(ENERGY_TOKEN_WEIGHT_OUTPUT),
        # The class constant is per output-equivalent Mtok, so the input figure is
        # it x the input weight. Both are given so neither has to be inferred.
        "energy_wh_per_mtok_input": _f(wh_per_mtok * ENERGY_TOKEN_WEIGHT_INPUT, 3),
        "energy_wh_per_mtok_output": _f(wh_per_mtok * ENERGY_TOKEN_WEIGHT_OUTPUT, 3),
        "output_to_input_energy_ratio": float(OUTPUT_TO_INPUT_ENERGY_RATIO),
        "tokens": {
            "input": int(input_tokens),
            "output": int(output_tokens),
            "cache_read": int(cache_read_tokens),
            "cache_write": int(cache_write_tokens),
        },
        # Compute Wh per bucket; sums to energy_wh.
        "energy_wh_by_bucket": {k: _f(v) for k, v in by_bucket.items()},
        "reasoning_tier": reasoning_tier,
        # ── added: factor resolution ──
        "pue_profile": profile,
        "pue_disclosure": factors.pue.disclosure,
        "grid_co2e_basis": grid_basis,
        # Which precedence rule chose the grid factor, as a stable key
        # (`provider:anthropic` | `local_setting` | `global_default` |
        # `run_override`, or — once a workspace/managed/harness layer is in
        # play — `workspace` | `workspace:provider:<p>` | `managed:<name>` |
        # `managed:<name>:provider:<p>` | `harness` | `harness:provider:<p>`),
        # and the operator's own label for it when they set one. A run
        # recorded before these existed carries neither — read them as
        # unknown, never as `global_default`.
        "grid_co2e_source": factors.grid.source,
        "grid_co2e_label": factors.grid.label,
        # Which *layer* of the ladder chose it — coarser than `grid_co2e_source`
        # (folds every provider-specific win at a layer into that layer's name)
        # and always one of `run_override`, `harness`, `workspace`, `managed`,
        # `env`, `global_default`.
        "grid_co2e_layer": factors.grid.layer,
        # Phase 3, additive: an operator-pinned region for this provider
        # (tret.services.grid_regions), or None when none applied to this
        # win — see FactorSet.grid.region / `_resolve_grid`.
        "grid_region": factors.grid.region,
        "grid_requested_region": factors.grid.requested_region,
        "grid_region_resolution_status": factors.grid.region_resolution_status,
        # "annual_average" (the plain per-provider/default figure, or a
        # table referenced but not evaluated/missed) or "hourly" (an hourly
        # grid.tables entry had a value for this run's actual start time) —
        # see tret.services.grid_tables and `_apply_grid_table`.
        "grid_temporal": factors.grid.temporal or "annual_average",
        "grid_factor_boundary": factors.grid.factor_boundary,
        "grid_gas_coverage": factors.grid.gas_coverage,
        "grid_gwp_horizon_years": factors.grid.gwp_horizon_years,
        "grid_gwp_assessment_basis": factors.grid.gwp_assessment_basis,
        "grid_includes_td_losses": factors.grid.includes_td_losses,
        "grid_electricity_mix_basis": factors.grid.electricity_mix_basis,
        "grid_dataset_version": factors.grid.dataset_version,
        "grid_observation_year": factors.grid.observation_year,
        # ── added: money and uncertainty ──
        "cost": _cost_block(cost_usd, baseline),
        "uncertainty": uncertainty_band(
            total_g,
            compute_wh,
            total_wh,
            reasoning_tier=reasoning_tier,
            deployment=deployment,
            settings=settings,
            band=(factors.band_low.value, factors.band_high.value),
            measured=is_measured,
            evidence=_band_evidence(factors, validated_evidence) if pue_is_applied else None,
            pue_applied=pue_is_applied,
        ),
        # ── added: provenance ──
        "factors": actual_factor_records,
        # Which layers contributed anything to this run at all, most specific
        # first — e.g. `["workspace", "env"]` when a workspace overrode the
        # grid factor but every other constant fell through to the
        # environment. `["global_default"]` when nothing was ever configured.
        "factor_layers": list(factors.layers_present),
        "caveats": caveat_records(
            reasoning_tier=reasoning_tier,
            deployment=deployment,
            cost_usd=cost_usd,
            grid_basis=grid_basis,
            baseline_grid_basis=baseline.get("grid_co2e_basis"),
            baseline_grid_compatible=baseline.get("carbon_compatible_with_actual"),
            active_params_unknown=active_params_unknown,
            active_params_gpu_only=active_params_gpu_only,
            measured=is_measured,
            energy_strategy=method_id,
            embodied_double_add_prevented=embodied_double_add_prevented,
        ),
    }
    if embodied_allocation is not None:
        result["embodied_allocation"] = embodied_allocation
    for caveat in result["caveats"]:
        if caveat.get("key") == "reasoning_token_accounting":
            caveat.update({
                "key": "reasoning_counted_in_output" if reasoning_accounting == "counted_in_output" else "reasoning_hidden",
                "direction": "either",
                "label": "Reasoning count is included in output" if reasoning_accounting == "counted_in_output" else "Reasoning denominator uncertainty",
                "note": (
                    "Provider-reported reasoning included in output is counted once. "
                    if reasoning_accounting == "counted_in_output" else
                    "Confirmed additional reasoning is counted once in the energy denominator. "
                    if reasoning_accounting == "additional" else
                    "The provider did not establish a separate reasoning count; missing does not mean zero. "
                ) + "The calibration's reasoning denominator remains unresolved, so the direction of model error is unknown.",
            })
    if reasoning_tokens is not None or reasoning_accounting != "unknown":
        result["reasoning_tokens"] = {
            "known_subset": int(reasoning_tokens) if reasoning_tokens is not None else None,
            "accounting": reasoning_accounting,
            "caveat": "Billed output is unchanged; only confirmed additional reasoning enters the separate energy denominator.",
        }
    from tret.services.emissions_coverage import operational_coverage

    result["coverage"] = operational_coverage(result)
    return result


# ── reading a stored block back ──────────────────────────────────────────────
def energy_wh_field(energy_wh: Decimal | float | None) -> float | None:
    """A run's stored `energy_wh` column as a JSON number — or None, never 0.

    One line, copied at five call sites before this existed (api/runs.py,
    api/chat.py, services/export.py, engine/harness.py, engine/tools.py). The
    conditional is the whole point and is easy to drop when copying: `float(None)`
    raises, so the tempting `float(run.energy_wh or 0)` "fix" turns "this run has
    no estimate" into "this run drew no power", which is the one claim tret must
    never make by accident. It lives here, next to `emission_summary_fields`,
    because the null-not-zero rule is the same rule that function exists to
    enforce.

    Deliberately NOT folded into `emission_summary_fields`: that function reads
    the run's stored `energy_accounting` block, while this reads the `energy_wh`
    *column*. They are written together (engine/harness.py sets the column from
    `accounting["energy_wh"]`) but they are not the same field, and two of the
    five call sites — the `done` SSE event and the `run_harness_task` result —
    carry the column without carrying a summary block at all.
    """
    return float(energy_wh) if energy_wh is not None else None


def emission_summary_fields(accounting: dict | None) -> dict[str, Any]:
    """The carbon fields a run *summary* carries, read as recorded.

    Missing means missing: a run with no estimate (or one recorded before scopes
    existed) reports None, never 0. `None != 0` is the whole point — a zero would
    claim the run emitted nothing.
    """
    accounting = accounting or {}
    scopes = accounting.get("scopes") or {}
    baseline = accounting.get("baseline") or {}
    band = accounting.get("uncertainty") or {}
    return {
        "co2e_g": accounting.get("co2e_g"),
        "scope2_g": scopes.get("scope2_g"),
        "scope3_g": scopes.get("scope3_g"),
        "avoided_co2e_g": baseline.get("avoided_co2e_g"),
        # Added: money saved, and the band around the carbon figure. Null on any
        # run recorded before they existed, for the same reason as the above.
        "avoided_usd": baseline.get("avoided_usd"),
        # Added: the share of frontier spend avoided. Signed like avoided_usd;
        # null (never 0%) when there is no baseline, the baseline itself costs
        # nothing, or cost data is missing — see _baseline_block.
        "avoided_usd_pct": baseline.get("avoided_usd_pct"),
        "co2e_g_low": band.get("co2e_g_low"),
        "co2e_g_high": band.get("co2e_g_high"),
    }


def emission_event_fields(accounting: dict | None) -> dict[str, Any]:
    """The carbon fields the SSE `usage`/`done` events carry. Nulls stay null."""
    accounting = accounting or {}
    scopes = accounting.get("scopes") or {}
    baseline = accounting.get("baseline") or {}
    band = accounting.get("uncertainty") or {}
    return {
        "co2e_g": accounting.get("co2e_g"),
        "scope2_g": scopes.get("scope2_g"),
        "scope3_g": scopes.get("scope3_g"),
        "baseline_co2e_g": baseline.get("co2e_g"),
        "avoided_co2e_g": baseline.get("avoided_co2e_g"),
        # Added, same additive rule as the summary.
        "avoided_usd": baseline.get("avoided_usd"),
        "avoided_usd_pct": baseline.get("avoided_usd_pct"),
        "co2e_g_low": band.get("co2e_g_low"),
        "co2e_g_high": band.get("co2e_g_high"),
    }


# ── rolling several models' accounting into one run ──────────────────────────
# A run that changes model part-way (engine/supervisor.py) produces one
# accounting block per segment. `runs.energy_accounting` still has to answer
# "what did this run cost the planet", and the answer cannot be one segment's
# block: every per-model factor in it — energy class, PUE, grid intensity,
# baseline — would then be asserted of tokens that were never spent on that
# model.
#
# The rule is the one `api/analytics.py` already applies when rolling carbon
# across runs: sum what is genuinely additive, and where segments disagree on a
# factor, report null rather than inventing a figure. A null here means "these
# segments were accounted differently"; it never means zero.

# Quantities that add across models unconditionally: a kWh is a kWh, a token is
# a token, and a dollar is a dollar whatever grid generated the electricity.
_SUMMABLE_TOP = (
    "energy_wh",
    "energy_wh_total",
    "energy_wh_estimated",
    "weighted_tokens",
)
_SUMMABLE_NESTED = {
    "tokens": ("input", "output", "cache_read", "cache_write"),
    "energy_wh_by_bucket": ("input", "output", "cache_read", "cache_write"),
    # Baseline *energy* only. `resolve_baseline_model` picks one global baseline
    # model, so baseline energy is a linear function of tokens against a single
    # model and genuinely adds. Baseline carbon is carbon and follows the rule
    # below.
    "baseline": ("energy_wh", "energy_wh_total"),
    "cost": ("usd", "baseline_usd", "avoided_usd"),
    # The band's *energy* edges add like energy does. Its carbon edges are
    # carbon and follow the basis rule below. Everything else in the block
    # (band factors, kind, derivation, contributions) is kept only where the
    # segments agree — a run that ran half at one band and half at another has
    # no single band factor to report, and `_agreed` nulls it.
    "uncertainty": (
        "energy_wh_low",
        "energy_wh_high",
        "energy_wh_total_low",
        "energy_wh_total_high",
    ),
}

# ── carbon may only be summed within one GHG Protocol basis ──────────────────
# Location-based and market-based figures answer different questions and may not
# be added. `api/analytics.py` already enforces this at window scale — see
# `_is_summable` / `_bucket_bases` / `by_basis` there, and the rule stated in
# EMISSIONS_DISCLAIMER — and this is the same rule at run scale.
#
# It was previously enforced only halfway here, which was worse than not at all:
# `grid_co2e_basis` was nulled because the segments disagreed, while `co2e_g`
# kept its sum. A null basis beside a populated carbon figure reads as "the basis
# wasn't recorded", not "this addition is not legitimate" — so the number
# survived and the one field that would have exposed it was removed. Latent on a
# default deployment, where every segment shares the global factor; live for an
# operator who has configured TRET_GRID_FACTORS per provider, which is exactly
# the operator most likely to publish the figure.
#
# `embodied_g` is deliberately absent: it is hardware amortization, not
# electricity, so no grid basis applies to it and it adds freely.
_CARBON_TOP = ("co2e_g", "co2e_g_low", "co2e_g_high")
_CARBON_NESTED = {
    "scopes": ("scope1_g", "scope2_g", "scope3_g"),
    "baseline": ("co2e_g", "avoided_co2e_g"),
    "uncertainty": ("co2e_g_low", "co2e_g_high"),
}


def _bases_of(blocks: list[dict]) -> list:
    """The distinct GHG Protocol bases these blocks were accounted under."""
    seen = {b.get("grid_co2e_basis") for b in blocks}
    return sorted(seen, key=lambda basis: (basis is None, str(basis)))


CROSS_BASIS_CAVEAT = {
    "key": "carbon_crosses_grid_basis",
    "label": "Carbon could not be summed: this run uses incompatible grid factors",
    "direction": "unknown",
    "note": (
        "Parts of this run use different GHG Protocol bases or grid-factor "
        "methodologies. Those figures may not be added, so every combined carbon "
        "figure here is null and compatible subtotals are in `by_basis`. "
        "Energy, tokens and cost are unaffected and are summed as normal."
    ),
}

LIST_PRICE_CAVEAT = {
    "key": "cost_is_list_price_only",
    "label": "Cost is list-price API spend",
    "direction": "understates",
    "note": (
        "Summed cost counts what providers bill for tokens. It excludes "
        "electricity and hardware amortization for locally-executed inference, so "
        "a total mixing a cloud call with a local one understates real cost."
    ),
}

MULTI_MODEL_CAVEAT = {
    "key": "multi_model_run",
    "label": "This run used more than one model",
    "direction": "unknown",
    "note": (
        "Energy, carbon, tokens and cost are summed across every model this run "
        "used; each model's own accounting is recorded separately in "
        "runs.model_timeline. Per-model factors (energy class, PUE, grid "
        "intensity, baseline) are reported only where every segment agreed — "
        "elsewhere they are null, which means 'accounted differently', not zero."
    ),
}


def _sum_or_none(values: list) -> float | None:
    """Sum, unless any segment is missing the figure entirely.

    A partial sum would read as a complete one. Consistent with the rest of this
    module: a missing estimate is null, never zero.
    """
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(numbers) != len(values):
        return None
    total = sum(numbers)
    # Token counts stay integers: a run that used 3000 input tokens did not use
    # 3000.0 of them, and the difference shows up in every JSON payload.
    return total if all(isinstance(v, int) for v in numbers) else round(float(total), 6)


def _agreed(values: list):
    """The shared value, or None where the segments disagree."""
    first = values[0]
    try:
        return first if all(v == first for v in values) else None
    except Exception:  # noqa: BLE001 - unorderable/odd values are simply "mixed"
        return None


def grid_comparison_signature(accounting: dict) -> tuple:
    """Comparable grid method, or exact factor identity when metadata is unknown."""
    metadata = (
        accounting.get("grid_co2e_basis"),
        accounting.get("grid_factor_boundary"),
        accounting.get("grid_gas_coverage"),
        accounting.get("grid_gwp_horizon_years"),
        accounting.get("grid_gwp_assessment_basis"),
        accounting.get("grid_includes_td_losses"),
        accounting.get("grid_electricity_mix_basis"),
        accounting.get("grid_dataset_version"),
        accounting.get("grid_observation_year"),
    )
    unknown = (None, "", "unknown", "unspecified")
    if all(value not in unknown for value in metadata):
        return ("method", *metadata)
    # Every "we don't know" spelling (None, "", "unknown", "unspecified")
    # collapses to one canonical sentinel before joining the identity tuple.
    # Without this, two runs that are each unknown in the same field —
    # overwhelmingly `grid_includes_td_losses` on the shipped default, which
    # has never been surveyed — could still compare "different" purely
    # because one recorded `None` and the other `"unspecified"`, which would
    # make even two runs priced under the identical default un-summable.
    # Fields that carry a real value are untouched: identity still requires
    # an exact match on grid_co2e_g_per_kwh/basis and on any metadata field
    # that is actually known. `grid_co2e_source`, `grid_co2e_label` and
    # `grid_co2e_layer` are provenance (where the number came from), not part
    # of what the number means, so they never enter the identity tuple: two
    # records recording the identical value/basis/metadata are the same
    # factor even if one was tagged with a layer and the other predates that
    # field entirely.
    normalized_metadata = tuple(
        "unknown" if value in unknown else value for value in metadata
    )
    return (
        "identity",
        accounting.get("grid_co2e_g_per_kwh"),
        *normalized_metadata,
    )


def combine_accountings(blocks: list[dict]) -> dict | None:
    """One run-level accounting block from several per-model ones.

    A single block is returned **unchanged**, byte for byte — the overwhelming
    majority of runs use one model, and none of them should acquire a different
    accounting record because this function exists.
    """
    blocks = [b for b in blocks if b]
    if not blocks:
        return None
    if len(blocks) == 1:
        return blocks[0]

    bases = _bases_of(blocks)
    factor_signatures = {grid_comparison_signature(block) for block in blocks}
    # One basis (or one that simply never varied) — carbon adds. More than one,
    # and it does not, at any scale.
    carbon_summable = len(bases) <= 1 and len(factor_signatures) <= 1

    combined = dict(blocks[0])
    for key in _SUMMABLE_TOP:
        combined[key] = _sum_or_none([b.get(key) for b in blocks])
    for key in _CARBON_TOP:
        combined[key] = (
            _sum_or_none([b.get(key) for b in blocks]) if carbon_summable else None
        )
    combined["embodied_g"] = _sum_or_none([b.get("embodied_g") for b in blocks])

    for parent, fields in _SUMMABLE_NESTED.items():
        children = [b.get(parent) or {} for b in blocks]
        merged = dict(children[0])
        carbon_fields = _CARBON_NESTED.get(parent, ())
        for field_name in fields:
            merged[field_name] = _sum_or_none([c.get(field_name) for c in children])
        for field_name in carbon_fields:
            merged[field_name] = (
                _sum_or_none([c.get(field_name) for c in children])
                if carbon_summable
                else None
            )
        for field_name in set(merged) - set(fields) - set(carbon_fields):
            merged[field_name] = _agreed([c.get(field_name) for c in children])
        combined[parent] = merged
    for parent, carbon_fields in _CARBON_NESTED.items():
        if parent in _SUMMABLE_NESTED:
            continue
        children = [b.get(parent) or {} for b in blocks]
        merged = dict(children[0])
        for field_name in carbon_fields:
            merged[field_name] = (
                _sum_or_none([c.get(field_name) for c in children])
                if carbon_summable
                else None
            )
        for field_name in set(merged) - set(carbon_fields):
            merged[field_name] = _agreed([c.get(field_name) for c in children])
        combined[parent] = merged

    # Every per-model factor: kept where the segments agree, nulled where they do
    # not. Nulling is the honest answer — "this run ran at PUE 1.2" is false if
    # half of it ran somewhere else.
    handled = (
        set(_SUMMABLE_TOP)
        | set(_SUMMABLE_NESTED)
        | set(_CARBON_TOP)
        | set(_CARBON_NESTED)
        | {"embodied_g"}
    )
    for key in set(combined) - handled:
        if key in (
            "estimated", "basis", "factors", "caveats", "factor_layers", "energy_source",
            "energy_meter",
        ):
            continue
        combined[key] = _agreed([b.get(key) for b in blocks])

    # "measured" only when every segment was; "mixed" when some but not all
    # were (a run that measured one leg and estimated another is neither
    # cleanly measured nor cleanly estimated); "estimated" otherwise. A block
    # from before this key existed reads as "estimated", same as
    # `energy_accounting` itself has always defaulted.
    # A segment that did no work (a model the run switched to and then never
    # used, or an aborted first turn) has nothing to be measured or estimated
    # and must not turn a fully metered run "mixed"; only segments that spent
    # energy count. If none did, the run reads as "estimated".
    worked = [b for b in blocks if (b.get("energy_wh") or 0) > 0]
    sources = [b.get("energy_source", "estimated") for b in (worked or blocks)]
    if all(s == "measured" for s in sources):
        combined["energy_source"] = "measured"
    elif any(s == "measured" for s in sources):
        combined["energy_source"] = "mixed"
    else:
        combined["energy_source"] = "estimated"

    boundaries = {b.get("energy_boundary", "unknown") for b in (worked or blocks)}
    combined["energy_boundary"] = next(iter(boundaries)) if len(boundaries) == 1 else "mixed"
    combined["energy_boundary_complete"] = all(
        b.get("energy_boundary_complete", False) for b in (worked or blocks)
    )
    methods = {b.get("method_id", "legacy_unknown") for b in (worked or blocks)}
    combined["method_id"] = next(iter(methods)) if len(methods) == 1 else "mixed"
    combined["included_components"] = list(dict.fromkeys(
        component
        for b in (worked or blocks)
        for component in b.get("included_components", [])
    ))
    combined["excluded_components"] = list(dict.fromkeys(
        component
        for b in (worked or blocks)
        for component in b.get("excluded_components", [])
        # A segment that excluded a component and another that included it
        # is not a contradiction to surface: the record declares
        # `component_lists_describe: "result"`, and the union result did
        # include it (some segment carried it), so included wins.
        if component not in combined["included_components"]
    ))
    component_sources: dict[str, set[str]] = {}
    for block in (worked or blocks):
        for component, source in block.get("energy_source_by_component", {}).items():
            component_sources.setdefault(component, set()).add(source)
    combined["energy_source_by_component"] = {
        component: next(iter(sources)) if len(sources) == 1 else "mixed"
        for component, sources in component_sources.items()
    }

    # `energy_meter` (engine/harness.py, tret/services/energy_meter.py): only
    # present on a segment whose model actually got metered. Additive, and
    # combined the same spirit as everything above — sum what is genuinely
    # additive (samples taken, seconds spent sampling), keep `kind`/
    # `interval_s` only where every metered segment agrees, and
    # `shared_device` is `True` if it was true for ANY segment: one shared
    # host-level reading in the mix is enough for the caveat it carries to
    # apply to the run as a whole.
    metered = [b.get("energy_meter") for b in blocks if b.get("energy_meter")]
    if metered:
        # `note` is unioned, not agreed: a short-segment note from one leg
        # ("fewer than 2 periodic samples...") and a clean `None` from another
        # are not a disagreement to be nulled out — both, if present, are
        # genuinely true of their own segment. Distinct non-empty notes are
        # joined in encounter order; an all-`None` mix joins to `None`, same
        # as `_agreed` would have.
        notes = list(
            dict.fromkeys(m.get("note") for m in metered if m.get("note"))
        )
        combined["energy_meter"] = {
            "kind": _agreed([m.get("kind") for m in metered]),
            "samples": _sum_or_none([m.get("samples") for m in metered]),
            "duration_s": _sum_or_none([m.get("duration_s") for m in metered]),
            "interval_s": _agreed([m.get("interval_s") for m in metered]),
            "shared_device": any(m.get("shared_device") for m in metered),
            "note": "; ".join(notes) if notes else None,
            "energy_boundary": _agreed([m.get("energy_boundary", "unknown") for m in metered])
            or "mixed",
        }

    combined["models"] = [b.get("model") for b in blocks]
    distinct_models = set(combined["models"])
    combined["grid_bases"] = bases
    combined["carbon_summable"] = carbon_summable
    if not carbon_summable:
        # Each subtotal is internally compatible. Basis alone is insufficient:
        # two custom factors can both have an unknown basis while representing
        # different conversion methods. Keep those in separate rows rather than
        # leaking the invalid sum through this fallback view.
        groups: dict[tuple, list[dict]] = {}
        for block in blocks:
            group_key = (
                block.get("grid_co2e_basis"),
                grid_comparison_signature(block),
            )
            groups.setdefault(group_key, []).append(block)
        combined["by_basis"] = [
            {
                "grid_co2e_basis": basis,
                "grid_factor_signature": list(signature),
                **{
                    key: _sum_or_none([b.get(key) for b in grouped_blocks])
                    for key in ("co2e_g", "energy_wh")
                },
                "models": [b.get("model") for b in grouped_blocks],
            }
            for (basis, signature), grouped_blocks in groups.items()
        ]
    combined["basis"] = (
        (
            "summed across the models this run used; per-model factors are reported "
            if len(distinct_models) > 1
            else "summed across calls to one model; call factors are reported "
        )
        + "only where every segment agreed"
        + (
            "; carbon is null because the segments use incompatible grid bases "
            "or factor methodologies and is broken out into compatible subtotals "
            "in `by_basis`. "
            if not carbon_summable
            else ". "
        )
        + str(blocks[0].get("basis", ""))
    )
    # Provenance annotations are unioned by key rather than summed: they describe
    # how a figure was reached, and every segment's reasoning still applies to
    # its own share.
    combined["factors"] = _union_by_key(blocks, "factors")
    caveats = [*_union_by_key(blocks, "caveats"), LIST_PRICE_CAVEAT]
    if len(distinct_models) > 1:
        caveats.append(MULTI_MODEL_CAVEAT)
    if not carbon_summable:
        caveats.append(CROSS_BASIS_CAVEAT)
    combined["caveats"] = caveats
    # Which layers were in play *anywhere* across the segments — deduped and
    # ordered by precedence (most specific first), since a run spanning
    # several models may have pulled its grid factor from a workspace override
    # on one segment and the environment on another. `FactorSet.layers_present`
    # already orders a single segment's own `factor_layers` this way (see
    # `build_factor_set`); a plain alphabetical `sorted()` here would put
    # "global_default" ahead of "workspace" and disagree with that ordering
    # (and with this field's own docstring above) the moment a run spans more
    # than one segment.
    from tret.services.emission_factors import LAYER_PRECEDENCE

    present = _union_by_key(blocks, "factor_layers")
    combined["factor_layers"] = sorted(
        present,
        key=lambda layer: (
            LAYER_PRECEDENCE.index(layer) if layer in LAYER_PRECEDENCE else len(LAYER_PRECEDENCE)
        ),
    )
    from tret.services.emissions_coverage import operational_coverage

    combined["coverage"] = operational_coverage(combined)
    return combined


def _union_by_key(blocks: list[dict], field_name: str) -> list:
    seen: dict = {}
    for block in blocks:
        for entry in block.get(field_name) or []:
            key = entry.get("key") if isinstance(entry, dict) else str(entry)
            previous = seen.get(key)
            if previous is None or previous == entry:
                seen.setdefault(key, entry)
            elif field_name == "factors" and isinstance(entry, dict):
                variants = previous.get("variants") if isinstance(previous, dict) else None
                if variants is None:
                    variants = [previous]
                if entry not in variants:
                    variants = [*variants, entry]
                seen[key] = {
                    "key": key,
                    "label": previous.get("label") if isinstance(previous, dict) else None,
                    "value": None,
                    "source": "mixed",
                    "note": "Component calls used different factor records; see variants.",
                    "variants": variants,
                }
    return list(seen.values())


# ── overhead: model calls a run makes about itself ───────────────────────────
# The router chooses which model runs the task; context compaction summarizes
# what it had to elide. Both are model calls, both cost real money and real
# electricity, and until they were metered neither appeared anywhere.
#
# They are accounted **separately from the run's own totals**, and not out of
# tidiness. Three things make folding them in unsound rather than merely coarse:
#
# * They run on a different model. The router runs on `TRET_ROUTER_MODEL`, the
#   summarizer on whatever cheap model the harness ceiling permits. Energy is
#   tokens x the *executing* model's energy class, and those classes differ by
#   more than an order of magnitude, so attributing overhead tokens at the task
#   model's class produces a wrong number, not an approximate one.
# * They may run on a different provider, and `TRET_GRID_FACTORS` is keyed by
#   provider — each entry carrying its own GHG Protocol basis. A run executing
#   locally whose routing happened at a cloud provider spans two bases, and
#   `api/analytics.py` already refuses to sum carbon across bases at window
#   scale. A single folded figure would perform exactly the addition that code
#   exists to prevent.
# * `runs.cost_usd` is already exposed through the runs API, exports and
#   deliverable provenance. Folding overhead in would leave every historical
#   value unchanged but change what it *means*, so a chart spanning the change
#   would show a cost jump that never happened — the same silent rewriting of
#   history that `analytics.emissions()` refuses when it reports as-recorded
#   figures instead of recomputing at today's factors.


def overhead_call(
    kind: str,
    model: ModelInfo,
    usage: Usage,
    *,
    factors: "FactorSet | None" = None,
    served_by: str | None = None,
) -> dict:
    """Account one overhead model call, in full, against its own model.

    Returns the same shape a run segment carries: the tokens, the money, and a
    complete `energy_accounting` block naming the model, its energy class, its
    provider's grid factor and that factor's basis. Callers persist it; nothing
    here writes.

    `factors`, when given, is passed straight through to `energy_accounting` —
    so a router or summarizer call started under the same layered
    configuration as the run it serves records its provenance the same way.
    Left `None` (every caller today), the call resolves its own factor set
    exactly as before.
    """
    accounting = energy_accounting(
        model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        factors=factors,
        energy_output_tokens=usage.energy_output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        reasoning_accounting=usage.reasoning_accounting,
    )
    extra = {"served_by": served_by} if served_by else {}
    return {
        "kind": kind,  # routing | compaction_summary
        "model": model.id,
        "provider": model.provider,
        **extra,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cost_usd": float(
            model.cost_usd(
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
            )
        ),
        "energy_wh": accounting["energy_wh"],
        "energy_accounting": accounting,
    }


def overhead_block(calls: list[dict]) -> dict | None:
    """Roll a run's overhead calls into one record. None when there were none.

    Money is summed unconditionally — a dollar is a dollar whatever grid it was
    generated on. Energy and carbon go through `combine_accountings`, which sums
    the additive quantities and nulls every per-model factor the calls disagreed
    on, so a routing call on one provider and a summarizer call on another
    produce a total with no invented energy class and no blended grid factor.
    """
    calls = [c for c in calls if c]
    if not calls:
        return None
    return {
        "calls": calls,
        "total_cost_usd": round(sum(c["cost_usd"] for c in calls), 6),
        "accounting": combine_accountings([c["energy_accounting"] for c in calls]),
    }
