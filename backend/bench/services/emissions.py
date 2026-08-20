"""Emissions accounting: energy, data-centre overhead, GHG Protocol scopes.

This module owns every carbon number bench prints. It grew out of
`providers/catalog.py`'s energy accounting, which still re-exports the names it
used to own so existing imports keep working.

What is here, and what each part is worth:

* **Energy classes** (`ENERGY_CLASS_WH_PER_MTOK`) — Wh per million
  *output-equivalent* tokens, calibrated by least-squares against the only
  granular public per-model dataset (Jegham et al. 2025, arXiv:2505.09598).
  Still an estimate; no longer a hand-picked one. `ENERGY_CLASS_CALIBRATION`
  carries the fit each class was anchored on so a reader can reproduce it.
* **Token weighting** — input and output tokens are *not* equally expensive.
  Prefill is parallel, generation is sequential, and the fit puts output at
  roughly 20x input per token. `ENERGY_TOKEN_WEIGHTS` holds the ratios; a cache
  read is a tenth of an input token, a cache write is a full prefill pass.
* **PUE** — data-centre overhead, resolved per deployment profile
  (hyperscaler cloud / workstation / on-prem facility). `energy_wh` stays the
  *compute* (IT-load) figure it has always been; `energy_wh_total` is
  compute x PUE.
* **Grid intensity** — a cited IEA global average by default, optionally
  replaced **per provider** by operator configuration (`BENCH_GRID_FACTORS`),
  carrying an explicit GHG Protocol **basis** label (location-based /
  market-based / unspecified) because mixing the two is meaningless, and a
  stable `grid_co2e_source` key saying *which* rule chose the factor. It is
  configuration, never inference: bench does not geolocate anything and makes
  no network call to resolve a factor (`GRID_NO_INFERENCE_NOTE`).
* **Scopes** — the GHG Protocol mapping for the *bench operator*: Scope 1 is
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

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from bench.config import (
    GRID_BASES,
    GRID_BASIS_LOCATION,
    GRID_BASIS_MARKET,
    GRID_BASIS_UNSPECIFIED,
    GridFactor,
    Settings,
    get_settings,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bench.providers.catalog import ModelCatalog, ModelInfo

# ── the calibration dataset ──────────────────────────────────────────────────
# Jegham, Abdelatti, Elmoubarki & Hendawi, "How Hungry is AI? Benchmarking
# Energy, Water, and Carbon Footprint of LLM Inference", arXiv:2505.09598,
# 14 May 2025. Wh per query at three prompt shapes — short 100in/300out, medium
# 1000in/1000out, long 10000in/1500out. The authors measured API latency and
# throughput and *inferred* the GPU class; it is the most granular per-model
# public data that exists, and it is still indirect.
#
# Fitting Wh = a x input + b x output by least squares over the three points per
# model (no intercept) gives the table below. bench's class constants are the
# fitted b values, and the input weight is the fitted b/a ratio. The working is
# reproduced in docs/emissions-methodology.md; `ENERGY_CLASS_CALIBRATION` below
# is the machine-readable version of the same table.
JEGHAM_2025 = {
    "citation": (
        "Jegham, Abdelatti, Elmoubarki & Hendawi, How Hungry is AI? Benchmarking "
        "Energy, Water, and Carbon Footprint of LLM Inference (arXiv:2505.09598)"
    ),
    "url": "https://arxiv.org/abs/2505.09598",
    "date": "2025-05-14",
    # (input tokens, output tokens) of the three published prompt shapes.
    "shapes": ((100, 300), (1000, 1000), (10000, 1500)),
    # model -> (Wh short, Wh medium, Wh long)
    "wh_per_query": {
        "GPT-4.1 nano": (0.10, 0.27, 0.45),
        "GPT-4o": (0.42, 1.21, 1.79),
        "Claude 3.7 Sonnet": (0.84, 2.78, 5.52),
        "o3": (7.03, 21.41, 39.22),
        "DeepSeek-R1": (23.82, 29.00, 33.63),
    },
    # Least-squares fit, Wh per million tokens: (a input, b output, b/a).
    # None marks a degenerate fit — see `_DEGENERATE_FIT_NOTE`.
    "fit_wh_per_mtok": {
        "GPT-4.1 nano": (4.2, 271.9, 65.1),
        "GPT-4o": (-6.1, 1233.1, None),
        "Claude 3.7 Sonnet": (156.7, 2634.7, 16.8),
        "o3": (792.8, 20850.4, 26.3),
        "DeepSeek-R1": (-1989.7, 35474.8, None),
    },
}

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
ENERGY_CLASS_WH_PER_MTOK: dict[str, Decimal] = {
    # GPT-4.1 nano fit: b = 271.9 Wh/Mtok. Small / distilled / nano-class served
    # models, and bench's default for local weights.
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
# discounted to a tenth of an input token — the same 0.1x bench prices it at. A
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
# bench does not implement it because no provider publishes active parameter
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
    "form": "Wh/output_token ~ alpha x active_params + beta, with exp decay in batch size",
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
# bench's cloud default of 1.2 is therefore mildly CONSERVATIVE — above all
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
# The three labels themselves are defined in bench/config.py (Settings has to
# validate against them and config.py cannot import this module) and re-exported
# here, which is where a reader looks for what they mean.
GRID_BASIS_MEANING = {
    GRID_BASIS_LOCATION: "the physical grid that served the load",
    GRID_BASIS_MARKET: "contractual renewable claims — PPAs, RECs, GOs",
    GRID_BASIS_UNSPECIFIED: "not stated; bench will not guess a basis on your behalf",
}

# ── which rule chose the factor ──────────────────────────────────────────────
# A run records not just the grid factor it used but *why* that factor applied,
# as a stable machine-readable key. Without it a provenance table can show the
# value and not the reason, which is the more interesting half when an operator
# has configured several factors and one run looks wrong.
#
# Precedence, highest first:
#   run_override    — a factor passed straight into the accounting call. No
#                     provenance and no basis: bench was handed a number.
#   provider:<name> — BENCH_GRID_FACTORS entry for the run's provider.
#   local_setting   — the legacy BENCH_LOCAL_GRID_CO2E_G_PER_KWH, on a
#                     self-hosted run.
#   global_default  — BENCH_GRID_CO2E_G_PER_KWH.
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
    "Precedence: BENCH_GRID_FACTORS entry for the run's provider, then "
    "BENCH_LOCAL_GRID_CO2E_G_PER_KWH for a self-hosted run (legacy, still "
    "honoured), then BENCH_GRID_CO2E_G_PER_KWH. A factor passed directly into the "
    "accounting call outranks all three and carries no basis claim."
)
# Why this is configuration and not geolocation. Recorded on the factor so the
# question a reviewer always asks is answered from the stored run.
GRID_NO_INFERENCE_NOTE = (
    "Operator configuration, never inference: bench does not derive a grid region "
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
        "value": 470.0,
        "source": "IEA, Electricity 2025 — 2024 global power-sector average",
        "url": "https://www.iea.org/reports/electricity-2025",
        "date": "2025-02",
        "note": "Reported as roughly 460-480 gCO2e/kWh; 470 is the midpoint.",
        "basis": GRID_BASIS_LOCATION,
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

# Regional sourcing options, documented rather than integrated. bench
# deliberately ships **no** external API call for grid intensity: a live
# dependency in the accounting path would make a stored run's carbon figure
# depend on a third party's uptime, and every one of these sources has licence
# or coverage limits an operator has to accept for themselves. The seam is
# configuration — BENCH_GRID_FACTORS (per provider), BENCH_LOCAL_GRID_CO2E_G_PER_KWH
# (legacy, self-hosted) and BENCH_GRID_CO2E_G_PER_KWH — into which an operator
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
# stays 0 (opt-in) because bench cannot see your hardware or its lifetime.
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
# estimate, so bench reports it as an explicit exclusion instead of picking a
# point in a 10,000x range.
TRAINING_AMORTIZATION_EXCLUDED = (
    "Excluded. Published per-query training amortizations span ~0.0001 to ~1.8 "
    "gCO2e/query — four orders of magnitude — driven almost entirely by the "
    "assumed number of queries a model serves over its life, which providers do "
    "not disclose. Including it would require the provider's total training "
    "energy, its grid mix at training time, and a defensible lifetime query "
    "count; bench has none of the three. Inference only."
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
# Published per-prompt figures bench's output can be sanity-checked against.
# None of them is a substitute for the class ladder (they cover one model each,
# on one operator's stack) but they bound the order of magnitude, and where
# bench lands relative to them is stated in docs/emissions-methodology.md.
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
            "Nothing to anchor against. bench's default models are Anthropic's, so "
            "the models bench is most likely to be running are the ones with the "
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
    "GHG Protocol scopes from the bench operator's perspective. Scope 1 = 0: "
    "running inference burns no fuel on the operator's own site, and a nonzero "
    "Scope 1 could only come from on-site generation, which bench cannot "
    "observe — so it is reported as an explicit zero rather than omitted. "
    "Scope 2 = purchased electricity for self-hosted (local) inference, where "
    "the operator buys the power. Scope 3 = cloud inference, which is a "
    "purchased service: the provider's own Scope 1/2 becomes the operator's "
    "Scope 3 (Cat. 1, purchased goods and services); plus amortized embodied "
    "hardware for local inference (Cat. 2, capital goods), which is 0 unless "
    "BENCH_EMBODIED_G_PER_RUN is set. Estimated from token counts, not an "
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
    "resolved (BENCH_EMISSIONS_BASELINE_MODEL may name a model the catalog does "
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
    "such runs. Excludes everything else bench does not bill through the token "
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


def wh_per_mtok_for_model(model: ModelInfo) -> Decimal:
    """The per-model energy constant, and the seam a better estimator plugs into.

    Today: an explicit `energy_wh_per_mtok` (a real measurement for the
    operator's own deployment) wins, otherwise the model's calibrated class
    constant. Tomorrow: an active-parameter model such as EcoLogits' fitted
    formula (`ECOLOGITS_ACTIVE_PARAM_MODEL`) would slot in here, reading an
    `active_params_b` off the catalog entry and falling back to the class ladder
    when it is unknown. Every caller goes through this function so that swap
    touches nothing else.
    """
    explicit = getattr(model, "energy_wh_per_mtok", None)
    if explicit is not None:
        return _d(explicit)
    return wh_per_mtok_for_class(getattr(model, "energy_class", DEFAULT_ENERGY_CLASS))


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
      direction for a facility bench cannot see.
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
    """(provider name, entry) from `BENCH_GRID_FACTORS`, or None.

    Keyed on the bench provider name, which is the only thing bench actually
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
        # A number handed to the accounting call. bench cannot state its
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
            "setting": f"BENCH_GRID_FACTORS[{name}]",
        }

    if deployment == DEPLOYMENT_LOCAL and settings.local_grid_co2e_g_per_kwh is not None:
        return {
            "value": float(settings.local_grid_co2e_g_per_kwh),
            "basis": normalize_grid_basis(settings.local_grid_co2e_basis),
            "source": GRID_SOURCE_LOCAL_SETTING,
            "rule": GRID_SOURCE_LOCAL_SETTING,
            "provider": provider,
            "label": None,
            "setting": "BENCH_LOCAL_GRID_CO2E_G_PER_KWH",
        }

    return {
        "value": float(settings.grid_co2e_g_per_kwh),
        "basis": normalize_grid_basis(settings.grid_co2e_basis),
        "source": GRID_SOURCE_GLOBAL_DEFAULT,
        "rule": GRID_SOURCE_GLOBAL_DEFAULT,
        "provider": provider,
        "label": None,
        "setting": "BENCH_GRID_CO2E_G_PER_KWH",
    }


def grid_factor_for(
    deployment: str, settings: Settings | None = None, *, provider: str | None = None
) -> float:
    """gCO2e/kWh to apply to this deployment's electricity.

    A per-provider `BENCH_GRID_FACTORS` entry wins when `provider` is given.
    Self-hosted inference may otherwise use the operator's own site/market-based
    factor (`local_grid_co2e_g_per_kwh`); unset, it falls back to the single
    `grid_co2e_g_per_kwh`. Cloud inference with no entry of its own always uses
    `grid_co2e_g_per_kwh`, since bench does not know which region served the
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
    assumed. An explicitly passed factor reports `unspecified`: bench was handed
    a number, not a provenance.
    """
    if overridden:
        return GRID_BASIS_UNSPECIFIED
    return str(resolve_grid_factor(provider, deployment, settings)["basis"])


def embodied_g_for(deployment: str, settings: Settings | None = None) -> Decimal:
    """Amortized embodied (capital-goods) carbon per run, in grams.

    Only self-hosted inference gets one: it is the operator's own hardware, so
    manufacturing it is their Scope 3 Cat. 2. Cloud hardware is embedded in the
    purchased service and is not separately estimated (bench has no basis for
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
    """A cited starting point for `BENCH_EMBODIED_G_PER_RUN`, in grams.

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


def uncertainty_contributions(*, reasoning_tier: bool, deployment: str) -> list[dict]:
    """Per-factor sensitivity: what moves if this input alone is wrong.

    Deliberately NOT combined into the headline band. Multiplying these together
    would give a band far wider than any published methodology claims, and
    presenting that as the answer would be its own kind of dishonesty. Grid
    intensity and the energy class dominate; the rest are second order.
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
                "average) against bench's 1.2 cloud default."
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
                "the billed output count bench reads, so real generation work can "
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
    return out


def uncertainty_band(
    co2e_g: Decimal,
    energy_wh: Decimal,
    energy_wh_total: Decimal,
    *,
    reasoning_tier: bool,
    deployment: str,
    settings: Settings | None = None,
) -> dict:
    """The judgment band around a run's figures. Never negative, never a CI."""
    low, high = band_factors(settings)
    co2e_low, co2e_high = _band(co2e_g, low, high)
    wh_low, wh_high = _band(energy_wh, low, high)
    total_low, total_high = _band(energy_wh_total, low, high)
    return {
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
        "contributions": uncertainty_contributions(
            reasoning_tier=reasoning_tier, deployment=deployment
        ),
        "basis": _UNCERTAINTY_BASIS,
    }


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
    wh_per_mtok: Decimal,
    pue: Decimal,
    pue_profile: str,
    grid: float,
    grid_basis: str,
    grid_overridden: bool,
    embodied_g: Decimal,
    deployment: str,
    settings: Settings,
    grid_source: str = GRID_SOURCE_GLOBAL_DEFAULT,
    grid_source_label: str | None = None,
    grid_setting: str | None = None,
) -> list[dict]:
    """Every constant that went into this run, with where it came from.

    The point of this list is that a reader of a stored run can answer "where did
    this number come from" for every input without consulting the code, the docs,
    or a frontend lookup table. It is generated from the same constants the
    arithmetic uses, so it cannot drift from them.
    """
    low, high = band_factors(settings)
    calibration = ENERGY_CLASS_CALIBRATION.get(energy_class, {})
    anchor = calibration.get("anchor_model")
    class_note = (
        f"Least-squares fit of Wh = a x input + b x output over the three published "
        f"prompt shapes for {anchor}; b = {calibration.get('fitted_wh_per_mtok')} "
        f"Wh/Mtok is what this class is anchored on."
        if anchor
        else (
            "No measured anchor for this class: interpolated one geometric step "
            "above the class below it. The weakest constant in the ladder."
        )
    )
    if is_reasoning_class(energy_class):
        class_note += (
            " Reasoning tier: several providers omit hidden thinking tokens from the "
            "billed output count bench reads, so energy per *visible* output token is "
            "inflated for these models. That bias is real and one-sided, not absorbed."
        )
    pue_ref = {
        PUE_PROFILE_CLOUD: PUE_REFERENCE["google"],
        PUE_PROFILE_ONPREM: PUE_REFERENCE["industry_average"],
    }.get(pue_profile)
    grid_ref = GRID_REFERENCE["default"]

    factors = [
        _factor(
            "energy_class",
            f"Energy class {energy_class}",
            _f(wh_per_mtok, 3),
            "Wh per million output-equivalent tokens",
            JEGHAM_2025["citation"] + " — least-squares fit by bench",
            JEGHAM_2025["url"],
            JEGHAM_2025["date"],
            "calibrated" if calibration.get("measured") else "low",
            class_note,
            "models.yaml: energy_class / energy_wh_per_mtok",
            anchor_model=anchor,
            measured_anchor=bool(calibration.get("measured")),
            reasoning_tier=is_reasoning_class(energy_class),
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
        ),
        _factor(
            "token_weight_cache_read",
            "Cache-read token weight",
            _f(ENERGY_CACHE_READ_MULTIPLIER, 4),
            "output-equivalent tokens per cached token read",
            "bench, by analogy with prompt-cache pricing",
            None,
            None,
            "structural",
            (
                "A tenth of an input token: a read re-uses stored KV state instead of a "
                "fresh forward pass, mirroring the 0.1x providers charge. Not zero — "
                "the state still has to be fetched and attended over."
            ),
        ),
        _factor(
            "token_weight_cache_write",
            "Cache-write token weight",
            _f(ENERGY_CACHE_WRITE_MULTIPLIER, 4),
            "output-equivalent tokens per cached token written",
            "bench, structural",
            None,
            None,
            "structural",
            "A cache write is a full prefill pass, so it weighs exactly what input does.",
        ),
        _factor(
            "pue",
            "Power Usage Effectiveness",
            _f(pue, 3),
            "total facility energy / IT-load energy",
            (pue_ref or {}).get("source", "bench default for a workstation profile"),
            (pue_ref or {}).get("url"),
            (pue_ref or {}).get("date"),
            "low",
            {
                PUE_PROFILE_CLOUD: (
                    "1.2 for hyperscaler cloud. Above every self-report (Google 1.09, "
                    "AWS 1.15, Microsoft 1.16) and well below the 1.56 industry "
                    "average, i.e. deliberately conservative for a facility bench "
                    "cannot see. Self-reported figures are fleet averages, not the "
                    "building that served this request."
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
            }[pue_profile],
            {
                PUE_PROFILE_CLOUD: "BENCH_DATACENTER_PUE",
                PUE_PROFILE_WORKSTATION: "BENCH_LOCAL_PUE",
                PUE_PROFILE_ONPREM: "BENCH_ONPREM_PUE",
            }[pue_profile],
            profile=pue_profile,
        ),
        _factor(
            "grid_intensity",
            "Grid carbon intensity",
            float(grid),
            "gCO2e/kWh",
            # Only claim the IEA as the source when the shipped IEA value is what
            # was actually applied. An operator's own figure is theirs to source —
            # and where they gave it a label, that label IS the source they cited.
            (
                grid_ref["source"]
                if grid_source == GRID_SOURCE_GLOBAL_DEFAULT
                and not grid_overridden
                and float(grid) == grid_ref["value"]
                else f"operator-supplied — {grid_source_label}"
                if grid_source_label
                else "operator-supplied"
            ),
            grid_ref["url"] if float(grid) == grid_ref["value"] else None,
            grid_ref["date"] if float(grid) == grid_ref["value"] else None,
            "low",
            (
                (
                    "Explicitly supplied for this run, so bench cannot state its "
                    "provenance or its GHG Protocol basis. "
                )
                if grid_overridden
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
                    "The operator's self-hosted factor (BENCH_LOCAL_GRID_CO2E_G_PER_KWH, "
                    "the legacy setting), applied because this run ran locally and its "
                    "provider has no BENCH_GRID_FACTORS entry. "
                )
                if grid_source == GRID_SOURCE_LOCAL_SETTING
                else (
                    f"The shipped default, {grid_ref['value']} gCO2e/kWh, is the IEA "
                    "2024 global power-sector average (reported as ~460-480; 470 is "
                    "the midpoint). "
                )
                if float(grid) == grid_ref["value"]
                else (
                    "Configured by the operator, replacing the shipped IEA global "
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
            ),
            "BENCH_EMBODIED_G_PER_RUN",
            gpu_h100_kg=EMBODIED_REFERENCE["gpu_h100_kg"],
            server_excluding_gpus_kg=EMBODIED_REFERENCE["server_excluding_gpus_kg"],
            lifetime_years=EMBODIED_REFERENCE["lifetime_years"],
            batch_size=EMBODIED_REFERENCE["batch_size"],
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
            "BENCH_UNCERTAINTY_BAND_LOW / BENCH_UNCERTAINTY_BAND_HIGH",
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
) -> list[dict]:
    """Named biases that travel with the figures instead of living only in a doc.

    Every one of these is a known way the number is wrong. They are structured
    rather than prose so a UI can surface the ones that apply to a given run.
    """
    out = [
        {
            "key": "reasoning_token_accounting",
            "label": "Reasoning tokens may not be in the counted output",
            "direction": "understates",
            "applies": True,
            "note": (
                "bench's token counts come from provider usage reporting. For several "
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
                "express that residual and bench does not pretend to."
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
                "service (Cat. 1) and bench has no basis for splitting it out, so a "
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
                "BENCH_EMBODIED_G_PER_RUN is set, embodied hardware) to this same "
                "run — so a run that reads 100% cheaper here can still carry a "
                "real, nonzero carbon figure. Money tracks what bench's token API "
                "bills; carbon tracks what running the model actually draws."
            ),
        },
        {
            "key": "baseline_crosses_grid_basis",
            "label": "The baseline comparison spans two GHG Protocol bases",
            "direction": "either",
            "applies": bool(
                grid_basis
                and baseline_grid_basis
                and grid_basis != baseline_grid_basis
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
                "cost bench already reports."
            ),
        },
    ]
    return [c for c in out if c["applies"]]


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
        from bench.providers.catalog import get_catalog

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
) -> dict:
    baseline = resolve_baseline_model(settings, catalog)
    if baseline is None:
        return _no_baseline()
    # The counterfactual is "these tokens through *that* model", so it is priced at
    # the factor that model's provider would have carried — including a
    # BENCH_GRID_FACTORS entry of its own. That is honest per-side, and it means the
    # two sides of the comparison can sit on different GHG Protocol bases; when they
    # do, the run carries the `baseline_crosses_grid_basis` caveat saying so.
    baseline_deployment = deployment_for(baseline.provider)
    baseline_grid = resolve_grid_factor(
        baseline.provider, baseline_deployment, settings, override=grid_g_per_kwh
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
            "grid_co2e_g_per_kwh": float(baseline_grid["value"]),
            "grid_co2e_basis": baseline_grid["basis"],
            "grid_co2e_source": baseline_grid["source"],
            "basis": (
                _BASELINE_BASIS + " This run used the baseline model itself, so avoided is 0."
            ),
        }
    deployment = baseline_deployment
    pue = pue_for(deployment, settings)
    grid = baseline_grid["value"]
    compute_wh = baseline.energy_wh(*tokens)
    total_wh = compute_wh * pue
    electricity_g = round(co2e_grams(total_wh, grid), _PLACES)
    embodied_g = round(embodied_g_for(deployment, settings), _PLACES)
    baseline_co2e = electricity_g + embodied_g
    avoided = baseline_co2e - round(actual_co2e_g, _PLACES)
    avoided_pct = _f(Decimal(100) * avoided / baseline_co2e, 3) if baseline_co2e > 0 else 0.0
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
        "avoided_co2e_g": _f(avoided),
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
        "grid_co2e_basis": baseline_grid["basis"],
        "grid_co2e_source": baseline_grid["source"],
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
) -> dict:
    """The auditable energy/carbon breakdown persisted on a run.

    Every field is an estimate derived from token counts, the model's calibrated
    energy class, a per-deployment PUE and a grid intensity — nothing here is
    metered. JSON-serializable throughout (floats, strings, lists; no Decimals).

    Key meanings, unchanged from the original contract:

    * `energy_wh` — **compute (IT-load) energy only**, no data-centre overhead.
    * `energy_wh_per_mtok` — Wh per million tokens, now per million
      *output-equivalent* tokens (see `weighted_tokens`); the chain
      `energy_wh = energy_wh_per_mtok x weighted_tokens / 1e6` is unchanged.
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
    `grid_co2e_label`, `cost`, `uncertainty`, `factors`, `caveats`.
    """
    settings = settings or get_settings()
    deployment = deployment_for(model.provider)
    pue = pue_for(deployment, settings)
    profile = pue_profile_for(deployment, settings)
    grid_overridden = grid_g_per_kwh is not None
    # One resolution, used for the arithmetic, the recorded factor, the provenance
    # record and the rollup key — so "which rule applied" cannot drift from "which
    # number was used".
    grid_resolution = resolve_grid_factor(
        model.provider, deployment, settings, override=grid_g_per_kwh
    )
    grid = grid_resolution["value"]
    grid_basis = grid_resolution["basis"]

    tokens = (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    wh_per_mtok = wh_per_mtok_for_model(model)
    energy_class = getattr(model, "energy_class", DEFAULT_ENERGY_CLASS)
    reasoning_tier = is_reasoning_class(energy_class)

    compute_wh = model.energy_wh(*tokens)
    by_bucket = energy_wh_by_bucket(wh_per_mtok, *tokens)
    total_wh = compute_wh * pue
    electricity_g = co2e_grams(total_wh, grid)
    embodied_g = embodied_g_for(deployment, settings)
    cost_usd = model.cost_usd(*tokens)

    scopes = scope_split(deployment, electricity_g, embodied_g)
    # Total is the sum of the *rounded* scopes, so the invariant
    # co2e_g == scope1 + scope2 + scope3 holds exactly rather than nearly.
    total_g = (
        Decimal(str(scopes["scope1_g"]))
        + Decimal(str(scopes["scope2_g"]))
        + Decimal(str(scopes["scope3_g"]))
    )

    baseline = _baseline_block(
        model,
        tokens,
        total_g,
        compute_wh,
        total_wh,
        cost_usd,
        grid_g_per_kwh,
        settings,
        catalog,
    )

    return {
        # ── original keys, original meanings ──
        "estimated": True,
        "model": model.id,
        "energy_class": energy_class,
        "energy_wh_per_mtok": float(wh_per_mtok),
        "weighted_tokens": float(weighted_tokens(*tokens)),
        "cache_read_weight": float(ENERGY_CACHE_READ_MULTIPLIER),
        "cache_write_weight": float(ENERGY_CACHE_WRITE_MULTIPLIER),
        "energy_wh": _f(compute_wh),  # compute / IT load only
        "grid_co2e_g_per_kwh": float(grid),
        "co2e_g": _f(total_g),  # == scope1 + scope2 + scope3
        "basis": _ACCOUNTING_BASIS,
        "pue": float(pue),
        "energy_wh_total": _f(total_wh),  # compute x PUE
        "deployment": deployment,
        "embodied_g": _f(embodied_g),
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
        "grid_co2e_basis": grid_basis,
        # Which precedence rule chose the grid factor, as a stable key
        # (`provider:anthropic` | `local_setting` | `global_default` |
        # `run_override`), and the operator's own label for it when they set one.
        # A run recorded before these existed carries neither — read them as
        # unknown, never as `global_default`.
        "grid_co2e_source": grid_resolution["source"],
        "grid_co2e_label": grid_resolution["label"],
        # ── added: money and uncertainty ──
        "cost": _cost_block(cost_usd, baseline),
        "uncertainty": uncertainty_band(
            total_g,
            compute_wh,
            total_wh,
            reasoning_tier=reasoning_tier,
            deployment=deployment,
            settings=settings,
        ),
        # ── added: provenance ──
        "factors": factor_records(
            energy_class=energy_class,
            wh_per_mtok=wh_per_mtok,
            pue=pue,
            pue_profile=profile,
            grid=float(grid),
            grid_basis=grid_basis,
            grid_overridden=grid_overridden,
            embodied_g=embodied_g,
            deployment=deployment,
            settings=settings,
            grid_source=grid_resolution["source"],
            grid_source_label=grid_resolution["label"],
            grid_setting=grid_resolution["setting"],
        ),
        "caveats": caveat_records(
            reasoning_tier=reasoning_tier,
            deployment=deployment,
            cost_usd=cost_usd,
            grid_basis=grid_basis,
            baseline_grid_basis=baseline.get("grid_co2e_basis"),
        ),
    }


# ── reading a stored block back ──────────────────────────────────────────────
def energy_wh_field(energy_wh: Decimal | float | None) -> float | None:
    """A run's stored `energy_wh` column as a JSON number — or None, never 0.

    One line, copied at five call sites before this existed (api/runs.py,
    api/chat.py, services/export.py, engine/harness.py, engine/tools.py). The
    conditional is the whole point and is easy to drop when copying: `float(None)`
    raises, so the tempting `float(run.energy_wh or 0)` "fix" turns "this run has
    no estimate" into "this run drew no power", which is the one claim bench must
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

# Quantities that add across models: energy is energy, a gram is a gram, and a
# token spent on one model is still a token.
_SUMMABLE_TOP = (
    "energy_wh",
    "energy_wh_total",
    "co2e_g",
    "embodied_g",
    "weighted_tokens",
)
_SUMMABLE_NESTED = {
    "tokens": ("input", "output", "cache_read", "cache_write"),
    "energy_wh_by_bucket": ("input", "output", "cache_read", "cache_write"),
    "scopes": ("scope1_g", "scope2_g", "scope3_g"),
    "baseline": ("energy_wh", "energy_wh_total", "co2e_g", "avoided_co2e_g"),
    "cost": ("usd", "baseline_usd", "avoided_usd"),
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

    combined = dict(blocks[0])
    for key in _SUMMABLE_TOP:
        combined[key] = _sum_or_none([b.get(key) for b in blocks])
    for parent, fields in _SUMMABLE_NESTED.items():
        children = [b.get(parent) or {} for b in blocks]
        merged = dict(children[0])
        for field_name in fields:
            merged[field_name] = _sum_or_none([c.get(field_name) for c in children])
        for field_name in set(merged) - set(fields):
            merged[field_name] = _agreed([c.get(field_name) for c in children])
        combined[parent] = merged

    # Every per-model factor: kept where the segments agree, nulled where they do
    # not. Nulling is the honest answer — "this run ran at PUE 1.2" is false if
    # half of it ran somewhere else.
    for key in set(combined) - set(_SUMMABLE_TOP) - set(_SUMMABLE_NESTED):
        if key in ("estimated", "basis", "factors", "caveats"):
            continue
        combined[key] = _agreed([b.get(key) for b in blocks])

    combined["models"] = [b.get("model") for b in blocks]
    combined["basis"] = (
        "summed across the models this run used; per-model factors are reported "
        "only where every segment agreed. " + str(blocks[0].get("basis", ""))
    )
    # Provenance annotations are unioned by key rather than summed: they describe
    # how a figure was reached, and every segment's reasoning still applies to
    # its own share.
    combined["factors"] = _union_by_key(blocks, "factors")
    combined["caveats"] = [*_union_by_key(blocks, "caveats"), MULTI_MODEL_CAVEAT]
    return combined


def _union_by_key(blocks: list[dict], field_name: str) -> list:
    seen: dict = {}
    for block in blocks:
        for entry in block.get(field_name) or []:
            key = entry.get("key") if isinstance(entry, dict) else str(entry)
            seen.setdefault(key, entry)
    return list(seen.values())
