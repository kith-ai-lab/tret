# Emissions methodology

How tret turns token counts into an energy, carbon and money figure; where every
constant came from; and — more important — what the resulting numbers are *not*
good for.

Implementation: `backend/tret/services/emissions.py`. Settings:
`backend/tret/config.py`. Per-model classes: `backend/tret/providers/models.yaml`.
The routing side of the energy model is in [eco-accounting.md](eco-accounting.md);
this page is authoritative wherever the two overlap.

**One-line summary: these are calibrated estimates for comparing model choices.
They are not measurements, not an inventory, and not reportable.**

## Read this first

Tret's flagship domain is climate risk, which is exactly why this page leads
with limits rather than headline numbers. A platform that produces TCFD sections
would not accept an unfalsifiable carbon claim from a portfolio company, so it
must not make one about itself.

Two things changed from tret's first version of this model, and both matter:

- The constants used to be **hand-picked**. They are now **fitted** to the only
  granular public per-model dataset that exists. That is a real improvement in
  provenance and a small one in accuracy.
- Every figure now travels with an explicit **uncertainty band** and a
  **per-factor provenance record**, so "where did this number come from" is
  answerable from a stored run alone.

What did not change: nothing here is metered, and the honest use of the output is
comparison between two model choices — never disclosure.

## The chain, end to end

```
weighted_tokens = 0.05*input + 1.0*output
                + 0.005*cache_read + 0.05*cache_write
energy_wh       = energy_wh_per_mtok * weighted_tokens / 1e6   # compute / IT load
energy_wh_total = energy_wh * PUE                              # + facility overhead
electricity_g   = energy_wh_total * grid_g_per_kwh / 1000
embodied_g      = embodied_g_per_run                           # local runs only, 0 by default
co2e_g          = electricity_g + embodied_g   # == scope1 + scope2 + scope3
co2e_g_low      = co2e_g / 2.5
co2e_g_high     = co2e_g * 2.5
avoided_co2e_g  = baseline_co2e_g - co2e_g     # signed
avoided_usd     = baseline_usd    - actual_usd # signed
```

`energy_wh` keeps the meaning it has always had — **compute (IT-load) energy
only**. `energy_wh_total` is the PUE-inclusive figure, and it is the one carbon
is derived from. Both are persisted, so the overhead is never hidden inside a
single number.

## Why input and output tokens are weighted apart

Tret used to weight an input token and an output token equally. That is wrong,
and measurably so. Prefill processes the whole prompt in parallel; generation is
autoregressive and pays a full forward pass per token. The fit below puts output
at roughly **20x** input per token.

The unit throughout is therefore an **output-equivalent token**:

| bucket | weight | why |
|---|---|---|
| output | 1.0 | the unit, by definition |
| input | 0.05 | 1/20, from the fit below |
| cache write | 0.05 | a full prefill pass |
| cache read | 0.005 | 0.1x input, as priced |

A cache read re-uses stored KV state instead of running a fresh forward pass, so
it gets the same 0.1x discount providers charge for it. It is not zero: the state
still has to be fetched and attended over.

## The regression, reproducibly

### The source data

Jegham, Abdelatti, Elmoubarki & Hendawi, *How Hungry is AI? Benchmarking Energy,
Water, and Carbon Footprint of LLM Inference*, [arXiv:2505.09598](https://arxiv.org/abs/2505.09598),
14 May 2025. The authors measured API latency and throughput and **inferred** the
GPU class — it is indirect, and it is the most granular per-model public data
available.

Wh per query at three prompt shapes: short 100 in / 300 out, medium 1000 in /
1000 out, long 10000 in / 1500 out.

| model | short | medium | long |
|---|---|---|---|
| GPT-4.1 nano | 0.10 | 0.27 | 0.45 |
| GPT-4o | 0.42 | 1.21 | 1.79 |
| Claude 3.7 Sonnet | 0.84 | 2.78 | 5.52 |
| o3 | 7.03 | 21.41 | 39.22 |
| DeepSeek-R1 | 23.82 | 29.00 | 33.63 |

The same table is in the code as `JEGHAM_2025` so the doc and the constants
cannot drift apart.

### The fit

Solve `Wh = a*input + b*output` by ordinary least squares over the three points
per model, with no intercept. The normal equations are the same for every model
because the shapes are:

```
Sxx = 100^2 + 1000^2 + 10000^2                      = 101,010,000
Sxy = 100*300 + 1000*1000 + 10000*1500              =  16,030,000
Syy = 300^2 + 1000^2 + 1500^2                       =   3,340,000
det = Sxx*Syy - Sxy^2                               = 8.04125e13

a = (Syi*Syy - Syo*Sxy) / det
b = (Syo*Sxx - Syi*Sxy) / det
```
where `Syi = sum(Wh_k * input_k)` and `Syo = sum(Wh_k * output_k)` over the three
shapes.

### The result

Wh per million tokens (a = input, b = output):

| model | a | b | b/a |
|---|---|---|---|
| GPT-4.1 nano | 4.2 | 271.9 | 65.1 |
| GPT-4o | **-6.1** | 1,233.1 | degenerate |
| Claude 3.7 Sonnet | 156.7 | 2,634.7 | 16.8 |
| o3 | 792.8 | 20,850.4 | 26.3 |
| DeepSeek-R1 | **-1,990** | 35,474.8 | degenerate |

**Two of five fits are degenerate**, and this is stated rather than smoothed
over. GPT-4o and DeepSeek-R1 solve to a *negative* input coefficient, which is
physically impossible — reading a token cannot generate energy. Three points and
two free parameters leave no room to absorb reporting noise or a shifting batch
size, and DeepSeek-R1's short-prompt figure (23.82 Wh, only 5.2 Wh below its
medium figure) dominates its residual. For those two models the fitted `b` is
kept and `a` is discarded.

**So the input weight is a documented assumption, not a measurement.** It is the
reciprocal of the geometric mean of the two well-behaved ratios — sqrt(16.8 x
26.3) = 21.0, rounded to 20, giving a weight of 0.05. GPT-4.1 nano's 65.1 is
excluded because its fitted `a` (4.2 Wh/Mtok) is within noise of zero, which
makes the ratio unstable rather than informative.

### Does the split fix the prompt-length artifact?

Partly, and here is the check. On a flat per-token basis the same models look 4-6x
cheaper on a long prompt than a short one — Claude 3.7 Sonnet reads 2,100 Wh/Mtok
at the short shape and 480 Wh/Mtok at the long one. A pure per-token linear model
cannot express that. Recomputing on an output-equivalent basis:

| model | flat spread | out-equiv spread |
|---|---|---|
| GPT-4.1 nano | 6.39x | 1.46x |
| GPT-4o | 6.75x | 1.54x |
| Claude 3.7 Sonnet | 4.38x | 1.04x |
| o3 | 5.15x | 1.18x |
| DeepSeek-R1 | 20.36x | 4.64x |

Most of the artifact resolves. **The residual is real and is not modelled**:
1.04-1.54x for the four well-behaved models, and 4.64x for DeepSeek-R1, whose
short-prompt figure is anomalously high — the same anomaly that makes its fit
degenerate. Tret records this as the `prompt_shape_residual` caveat on every
run rather than claiming the split solved it.

## The energy classes

Wh per million **output-equivalent** tokens. Each is a fitted `b` from the table
above, except XL.

| class | Wh/Mtok | anchor |
|---|---|---|
| S | 250 | GPT-4.1 nano (271.9) |
| M | 1,200 | GPT-4o (1,233.1) |
| L | 2,600 | Claude 3.7 Sonnet (2,634.7) |
| XL | 6,000 | **none — interpolated** |
| R | 21,000 | o3 (20,850.4) |

- **XL is the weakest constant in the ladder.** It has no measured anchor: it is
  one geometric step above L (x2.3, close to the x2.2 step from M to L) and well
  below R. The
  largest non-reasoning flagships are believed heavier than Sonnet-class, but no
  public dataset covers them.
- **R sits at the low end of the reasoning evidence.** o3 fits 20,850; DeepSeek-R1's
  (degenerate) fit implies 35,475. Choosing 21,000 is the *unsafe* direction, and
  it is chosen because o3's fit is the well-behaved one. A reasoning-heavy run may
  exceed this figure.
- **Class assignment for closed models is judgement.** Active parameter counts are
  unpublished, so tier and price stand in for size and do so imperfectly.
  `models.yaml` carries a rationale comment on every single assignment.

### Why the reasoning tier exists

o3 implies ~10,700 Wh/Mtok on a flat per-token basis at the medium shape — already
3.5x tret's old XL ceiling of 3,000, and 20,850 once input and output are
weighted apart. A reasoning model is not "a large model, a bit more"; it is a
different order of magnitude.

Reasoning is assigned from what a model *does*, never from what it costs. In the
reference dataset DeepSeek-R1 is among the two heaviest models measured **and**
among the cheapest models on the market. Price is not a proxy for this in either
direction, which is why tret's cheapest curated model (DeepSeek V4 Pro) carries
its heaviest energy class.

### The documented upgrade path

EcoLogits models per-token energy from **active parameter count** instead of a
class ladder, with published fitted constants α=1.17e-6, β=-1.12e-2, γ=4.05e-5 —
linear in active params, exponential decay in batch size, default batch 64.

Tret does not use it by default, because it would be fed a guessed parameter
count for every closed model in the catalog, and a precise-looking function
over a guessed input is worse than an openly coarse bucket — the class ladder
above stays the shipped default for exactly that reason. The seam described
here in earlier versions of this doc is now wired up rather than only
promised: `emissions.wh_per_mtok_for_model` still resolves every caller's
constant, and an operator who *does* have `active_params_b` for a model (their
own weights, most often) can opt a workspace into the formula. See
[Energy strategies and measured runs](#energy-strategies-and-measured-runs),
directly below, for what that opt-in actually computes and what it still gets
wrong.

Better data is also coming for open models: Hugging Face's AI Energy Score
measures ~166 models on identical H100s, and the ML.ENERGY benchmark
([arXiv:2505.06371](https://arxiv.org/abs/2505.06371)) covers open weights. Both
are stronger evidence than this ladder for the models they cover.

## Energy strategies and measured runs

Every run picks its per-token energy constant one of three ways —
`energy_strategy`, layered exactly like every other factor in
[Configuration layers](#configuration-layers) (`run_override > harness >
workspace > managed > global_default`; there is no `env` rung for this one
factor — no `TRET_*` setting sets it):

| strategy | what it does |
|---|---|
| `class_ladder` (default) | The table above: an explicit `models.yaml` constant, or the calibrated class. Unchanged. |
| `active_params` | EcoLogits' formula, below — only when the model also carries `active_params_b`. Falls back to `class_ladder` (with a caveat) when it does not. |
| `measured` | A config-time signal that this workspace's `model_overrides` are expected to be kept current. Does not by itself change the arithmetic beyond what a `model_overrides` entry already would. |

**Resolution order for one model, regardless of `energy_strategy`:**

1. A `model_overrides` entry for *this model id* — see below. Outranks
   everything, including an explicit `models.yaml` constant.
2. The model's own explicit `energy_wh_per_mtok` in `models.yaml` — unchanged
   from before this section existed.
3. The active-parameter formula, only when `energy_strategy` is
   `active_params` **and** the model has `active_params_b`.
4. The class ladder, as the fallback for both (3) not applying and the
   default case.

### `model_overrides`: an operator's own per-model constant

Any override document (workspace, managed, or the reserved harness layer —
see [Configuration layers](#configuration-layers) for the shape every other
key here follows) can carry a `model_overrides` block keyed by catalog model
id:

```json
{
  "model_overrides": {
    "local/qwen2.5:14b": {
      "energy_wh_per_mtok": 340.0,
      "label": "Metered on our own A6000 box, Q3 2025",
      "confidence": "measured",
      "url": "https://internal.example/energy-log",
      "as_of": "2025-09-01"
    }
  }
}
```

`label` is required, for the same reason every other override document here
requires one for a number it sets. `confidence` is `measured` (the default —
"I metered my own deployment"), `calibrated`, or `low`, and it becomes both
the record's `strategy` and its `confidence` — the whole point of this block
is an operator saying how sure they are about a number that replaces the
catalog default, and that word describes both how it was reached and how
much to trust it. A model with no entry here is unaffected; there is no
per-provider or default fallback the way `grid`/`pue` have — an override
names one model or it names nothing.

### The active-parameter formula, and its stated assumption

At `energy_strategy: "active_params"`, a model with `active_params_b` set is
priced by EcoLogits' published GPU energy model instead of the ladder:

```
E_gpu_per_output_token(Wh) = α × exp(β × B) × active_params_b + γ
```

at `batch_size (B) = 64` (the published default), converted to Wh per million
output-equivalent tokens (×1,000,000) to match the ladder's unit. At tret's
published constants (α=1.17e-6, β=-1.12e-2, γ=4.05e-5) and B=64, this yields
(Wh/Mtok, rounded): **44.5** at 7B active parameters, **80.5** at 70B, **140.5**
at 175B, **271.9** at 405B — positive and monotone in `active_params_b` for
every real model, with no floor anywhere in the range.

**The scope gap this strategy still has, stated plainly — this is why it is
`"low"` confidence, not `"calibrated"`.** EcoLogits' own `f_E` is per-GPU and
GPU-energy only: their published pipeline multiplies this per-GPU figure by
however many GPUs the model needs (derived from total parameter count and
per-GPU memory) and adds a separate server/host energy term before the result
is comparable to a whole-request figure. tret has neither of those inputs — no
GPU count, no server-energy term — and feeds this single-GPU number straight
into the same "whole-request Wh per Mtok" slot the class ladder fills. That is
a systematic **UNDERCOUNT**, not a rounding error: 271.9 Wh/Mtok for a
~405B-active-parameter model sits far below the L-class constant (2,600
Wh/Mtok, fitted from Claude 3.7 Sonnet — a comparably sized served model). Every
run this strategy prices carries an `active_params_gpu_only` caveat
(`direction: "understates"`) naming exactly this gap. Lifting this path to
`"calibrated"` would need the missing GPU-count term (computable from total
parameters and per-GPU memory, neither of which tret's catalog carries today)
and a server/host energy term added on top. A model with no `active_params_b`
under this strategy gets the class ladder instead, plus an
`active_params_unknown` caveat naming the fallback.

### Measured energy: an operator's own meter, on top of everything else

`energy_accounting(..., measured_energy_wh=<Wh>)` replaces the *estimate*
with a real IT-load figure for one run, on any deployment (self-hosted most
often, but nothing here requires it). What changes, and — as important —
what does not:

- `energy_wh` becomes the measurement. `energy_wh_estimated` keeps what the
  configured strategy would have produced (present on every run, measured or
  not — it just equals `energy_wh` when there is no measurement), and
  `energy_wh_by_bucket` is scaled proportionally from that estimate so the
  buckets still sum to the measured total.
- **PUE, grid intensity and embodied hardware still apply on top,
  unchanged.** A measurement is IT-load only — the same scope `energy_wh` has
  always had — never a substitute for facility overhead or hardware
  amortization. `co2e_g` is still `scope1_g + scope2_g + scope3_g`, computed
  from the measured (not estimated) electricity figure.
- The `energy_class` factor record's confidence becomes `"measured"`, its
  source `"Operator-supplied measurement (IT-load Wh)"`, and it carries
  `measured: true` — whatever strategy would otherwise have priced the
  (now-superseded) estimate is still named in the note.
- Two caveats flip to `applies: false`, and stay in the list rather than
  disappearing, so a reader can see they were considered:
  `unbatched_local_inference` (a real meter already reflects however batched
  or unbatched serving actually was) and `prompt_shape_residual` (there is no
  per-token model left to have a residual against). Every other caveat is
  unaffected.
- `uncertainty.contributions`' `energy_class` and `batching` rows narrow to
  0.9/1.1 (instrument-level tolerance, not the usual class/batch-size
  guesswork) — **the headline judgment band itself is unchanged this phase**;
  narrowing it is future work, not something this run alone should quietly
  decide.
- The top-level `energy_source` field is `"estimated"` or `"measured"`; a run
  spanning several models (`combine_accountings`) reports `"mixed"` unless
  every segment was measured.

`measured_energy_wh` must be `>= 0`; a negative value raises `ValueError`
rather than silently producing a negative energy figure.

#### How a measurement reaches a run

Two paths supply `measured_energy_wh`, for the two shapes a real meter
takes (`tret/services/energy_meter.py`):

- **`TRET_LOCAL_ENERGY_METER=nvidia_smi`** turns on automatic metering for
  every local (self-hosted) model segment a run passes through. Off by
  default — a run stays priced from tokens exactly as before. When on,
  `engine/harness.py` starts an `NvidiaSmiMeter` the moment a local segment
  begins and stops it the moment the segment ends (a model switch, or the
  run itself finishing), sampling `nvidia-smi --query-gpu=power.draw` every
  `TRET_LOCAL_ENERGY_METER_INTERVAL_S` (default 1.0s, floor 0.2s) and
  integrating watts x seconds into Wh. This is **host-level** power, not a
  per-process figure — `nvidia-smi` has no notion of "this one request's
  share" — so on a box running anything besides the one model server it
  **overstates** this run's actual draw; every such reading is recorded
  `shared_device: true` on the run's `energy_meter` block and carries the
  additive `shared_device_measurement` caveat (`direction: "overstates"`)
  saying so. It needs an NVIDIA GPU and driver reachable from the process
  that runs the harness — the NVIDIA Container Toolkit runtime, for Ollama
  running inside Docker — and is not supported on macOS at all (Apple
  Silicon's own `powermetrics` needs `sudo`, which a server process has no
  business asking an operator for). A missing binary, a parse failure, or
  the meter failing to start or stop never fails or measurably delays the
  run: it is logged once and the segment falls back to the per-token
  estimate, exactly as if metering were off.
- **An external reading**, for everything automatic metering cannot reach —
  a Mac's `powermetrics`, a smart PDU, a cluster's own accounting. Pass the
  Wh figure straight in: `tret.sdk.Router.run`/`arun(measured_energy_wh=…)`
  from the SDK, or `tret run --measured-wh <WH>` from the headless CLI
  (`tret/local_run.py`). Either way it reaches the same
  `energy_accounting(measured_energy_wh=…)` call described above — there is
  no meter object in the loop, just the number.

Both paths measure **IT-load only**. PUE, grid intensity and embodied
hardware still apply on top exactly as they do to an estimate — a
measurement replaces the per-token guess for compute energy; it is not a
substitute for facility overhead or hardware amortization, and it does not
change which deployment (and therefore which PUE profile and GHG Protocol
scope) a run is accounted under.

## Regions, hourly tables, hardware profiles, and a band that responds to evidence

Four small, independent additions to the override document described under
[Configuration layers](#configuration-layers) — each is its own opt-in, each
resolves through the identical layer ladder (`run_override > harness >
workspace > managed > env > dataset > global_default`) as everything else in this
document, and none of them changes a single figure unless an operator's own
document asks for it.

**Regions.** A `grid.providers` key may now be a bare provider
(`"anthropic"`) or a provider pinned to a region (`"anthropic@us-east"`), and
a document may separately carry `grid.regions`, a `{provider: region}` map:

```json
{
  "grid": {
    "regions": {"anthropic": "us-east"},
    "providers": {
      "anthropic": {"g_per_kwh": 400, "basis": "location_based", "label": "US average"},
      "anthropic@us-east": {"g_per_kwh": 90, "basis": "location_based", "label": "PJM East"}
    }
  }
}
```

The region itself is resolved exactly like every other factor here — the
most specific layer (`harness`, then `workspace`, then `managed`) that pins a
region for this provider wins — and the *resolved* region then applies to
the provider-entry lookup in **every** layer being checked, not only the one
that pinned it: a workspace-level `grid.regions` pin makes a managed layer's
`anthropic@us-east` entry win over its own bare `anthropic` entry, even
though the pin and the entry live in two different documents. A run's
`grid_region` (top level and on the `grid_intensity` factor record) names
the region actually used, or `null` when none applied. Every `grid.providers`
key — bare or pinned — is validated against the same `provider` /
`provider@region` shape; an unrecognised one is rejected by name (422).

**Hourly tables.** `grid.tables` names one or more operator-pasted CSV
tables of grid carbon intensity, and any `grid.default` or
`grid.providers.<key>` entry may reference one by name:

```json
{
  "grid": {
    "default": {
      "g_per_kwh": 410, "basis": "location_based",
      "label": "annual average, falls back when the table has no value",
      "table": "regional_hourly"
    },
    "tables": {
      "regional_hourly": {
        "label": "utility-published hourly intensity, 2025",
        "basis": "location_based",
        "csv": "timestamp_utc,g_per_kwh\n2025-01-01T00:00:00Z,520\n2025-01-01T01:00:00Z,505\n..."
      }
    }
  }
}
```

Two CSV shapes are accepted: a **diurnal** profile (`hour_utc,g_per_kwh`,
exactly the 24 rows 0-23 — a typical day with no dates, so every lookup
hits) or a dated **series** (`timestamp_utc,g_per_kwh`, strictly ascending).
A table is parsed and validated when the document is written — a malformed
CSV is rejected as `grid.tables.<name>.csv: <what was wrong>` — and again,
from a cache keyed on a content hash of the CSV text, its label and its
basis, so an unchanged table is never re-parsed, whenever a run needs its
value. That cache is bounded by total CSV characters resident (16,000,000
across every table any workspace has ever configured, oldest evicted first),
not by entry count, so it cannot grow without bound the way an entry-count
cache could. A table reference that names no table in the same document is
rejected as `grid.default.table` / `grid.providers.<key>.table references
unknown table '<name>'`; one that names a table whose own `basis` differs
from the referencing entry's is rejected too (`grid.default.table 't' has
basis market_based but the entry is location_based`, and the
`grid.providers.<key>.table` equivalent) — a run must never land in a
different GHG basis depending on which hour it happened to start in.

A document is capped at **8 tables**, with their CSV text summing to at most
**2,000,000 characters** combined (each table is separately capped at
600,000 characters on its own) — `grid.tables: at most 8 tables per
document` / `grid.tables: combined csv exceeds 2000000 characters` (422).
Validating a maximal document is real CPU (parsing every table's rows), so
both endpoints that accept one — `PUT /api/workspace/settings/emissions`
and `POST /api/analytics/emissions/whatif` — additionally refuse a request
body over **3MB** with 413, before that validation ever runs. A what-if
recompute over many stored runs validates its scenario, workspace, and
managed documents once per request rather than once per run, so a window
with hundreds of runs sharing one tabled document costs the same as a window
with one.

At the moment a run actually happens, `build_factor_set(..., at=<run's start
time, timezone-aware>)` looks the table up against `at`:

- **A diurnal table always hits** (there is always an hour of day to
  match). A series table returns the latest row at or before `at`, but only
  within a two-hour gap — past that, the entry's own `g_per_kwh` applies
  instead, exactly as if no table had been referenced.
- **A hit** replaces the entry's value with the table's, and the run records
  `grid_temporal: "hourly"` alongside the table's name and a summary (row
  count, value range, first/last timestamp for a series).
- **A miss, or no `at` at all**, keeps the entry's own `g_per_kwh` and
  records `grid_temporal: "annual_average"` — `at` omitted is exactly what
  the effective-factors GET below does, so a table that is configured but
  not yet evaluated against a specific run still shows up by name (a UI can
  say "an hourly table will apply at run time" without claiming to know
  which hour's value that will be).
- **`at` must be timezone-aware.** A naive datetime is ambiguous about which
  UTC offset it means, and guessing would be a silent local-time bug, so it
  raises `ValueError` instead. A stored `created_at` that round-trips naive
  — every deployment on SQLite, since it has no genuine timezone-aware
  storage, not only a row written before this feature existed — is treated
  as UTC by every caller that reads it (the runner's own `execute()`, the
  what-if recompute's `_aware_utc`) before it is ever handed to `at`, never
  guessed at any other offset and never silently substituted with "now".

Same stance as [Why this is configuration and not
geolocation](#why-this-is-configuration-and-not-geolocation): a region pin
and a table's rows are both entirely operator-supplied. Nothing here
geolocates a request, fetches a utility's published intensity feed, or
infers a table from a provider name — an operator who wants either pastes it
in.

**Hardware profiles.** `embodied.profile` describes a self-hosted deployment
— GPU count, expected lifetime run count, batch size, whether to amortize
the server chassis alongside the GPUs, which GPU model (`h100` is the only
one with a cited figure today) — instead of requiring the operator to work
out the resulting grams-per-run figure by hand:

```json
{
  "embodied": {
    "profile": {
      "gpus": 8, "runs_over_lifetime": 500000, "batch_size": 32,
      "label": "our own H100 box, 3-year depreciation"
    }
  }
}
```

`embodied.g_per_run` (a flat figure) and `embodied.profile` (a described
setup) are two complete, independent answers to "what does this run's
embodied carbon cost" — a document may set one or the other, never both
(`embodied: set g_per_run or profile, not both`, 422). Exactly like
`g_per_run`, a profile only ever amortizes against self-hosted inference —
a cloud run's embodied figure is unconditionally 0 regardless of what a
document's `embodied.profile` describes, because that hardware is not the
operator's. The resolved value is identical to what the shipped
`amortized_embodied_g_per_run` constants would produce for the same inputs;
the run's `embodied_hardware` factor record carries the whole profile
(inputs and the resulting grams/run) alongside the **same** `"placeholder"`
confidence and caveat text every embodied figure already carries — a
profile changes which numbers feed the arithmetic, not the confidence of
the H100/server constants underneath it. The constant is a placeholder
resting on a placeholder either way, and the record says so either way.

**A band that responds to evidence.** `band.derived: true` switches a run's
headline uncertainty band from the plain configured `low`/`high` to one
narrowed by whatever this specific run can actually prove:

```json
{"band": {"derived": true}}
```

Four conditions, each independently checkable off the run's own resolved
`FactorSet` — no new configuration beyond what a document already sets
elsewhere in this same layer ladder:

| Evidence | True when |
|---|---|
| `energy_measured` | this run's energy came from `measured_energy_wh`, not the per-token estimate |
| `pue_metered` | the winning PUE came from a labeled `workspace`/`managed`/`harness` layer |
| `grid_sourced_dated` | the winning grid factor came from a labeled `workspace`/`managed`/`harness` layer **and** carries an `as_of` date |
| `embodied_profiled` | the winning embodied figure came from a named hardware profile — recorded on the run today, but narrows no row: `uncertainty_contributions` ships no per-factor sensitivity row for embodied carbon to narrow |

Each true condition narrows the matching row(s) in `uncertainty.contributions`
to instrument-level tolerance (see [Per-factor
sensitivity](#per-factor-sensitivity) for what each row otherwise claims) —
`energy_measured` narrows `energy_class`, `batching`, `measurement_bias` and
(on a local run) `unbatched_local_inference`; `pue_metered` narrows `pue`;
`grid_sourced_dated` narrows `grid_intensity`. The (possibly narrowed) rows
are then folded into a band bounded **above** by the configured `low`/`high`
— never wider than configured, only ever at or inside it, and never *below*
it on an axis nothing actually evidenced: for each axis, the widest
remaining row's implied bound is compared against the configured value —
but only counts if that row was itself narrowed by evidence a moment ago;
an axis whose widest row is an ordinary, untouched baseline sensitivity
(nobody's evidence, just the row's own always-present uncertainty) stays at
the configured value rather than narrowing off it. This is deliberate, not
an oversight: `derived: true` with none of the four conditions true must
reproduce the plain configured band exactly (a deliberately conservative
4.0x/4.0x stays 4.0x/4.0x, never quietly tightens to whichever row's
baseline multiplier happens to be smallest), and one kind of evidence alone
(energy measured, say, with grid and PUE still unsourced) must not borrow
narrowing credit from `grid_intensity`'s own wide, undated default just
because that default happens to be the largest number left on the table.
`uncertainty` gains a `derivation` key naming the configured band, the one
actually applied, which rule chose it (`"configured"` when neither axis
narrowed, `"dominant_contribution"` when one did), and which row (if any)
governed the result. `band.derived` false or absent is exactly today's
behaviour: the configured band applies untouched and there is no
`derivation` key.

A worked example, for a **non-reasoning** model: a run whose energy was
measured, whose PUE came from a labeled workspace layer, and whose grid
factor came from a labeled, dated workspace layer (no hardware profile) —
against the shipped default 2.5x/2.5x band. `grid_intensity` narrows to
0.7/1.3, `pue` to 0.95/1.05, `energy_class`/`batching`/`measurement_bias` to
0.9/1.1 each, `token_energy_ratio`/`reasoning_tokens` untouched (1.15 and
1.2 respectively — smaller than grid's 1.3 either way). Every touched row is
evidence-stamped, and `grid_intensity` is the widest of them on both axes:
on the low axis its `1/0.7 ≈ 1.4286` is the largest divisor among the
touched rows and beats the configured 2.5, so it wins; on the high axis its
own `1.3` is likewise the largest touched multiplier and again beats 2.5.
The run reports `band_factor_low ≈ 1.4286`, `band_factor_high == 1.3`,
`derivation.rule == "dominant_contribution"`, `derivation.dominant_key ==
"grid_intensity"` — the grid factor's own sourcing was the single most
convincing piece of evidence this run had, and the band says so.

A **reasoning-tier** run under the identical evidence keeps the *high* axis
at the configured value instead: `reasoning_tokens`' own high_multiplier
jumps to `3.0` for a reasoning model (one-sided — hidden thinking tokens can
only make real generation work higher than what was counted, never lower),
which is wider than `grid_intensity`'s evidenced `1.3` and yet `evidence`
was never stamped on it (none of the four conditions touches
`reasoning_tokens`) — so per the rule above, the high axis stays at
whatever was configured rather than narrowing off `grid_intensity` in its
place. The low axis is unaffected (`reasoning_tokens`' `low_multiplier` is
`1.0` either way, never the widest divisor) and still narrows to
`grid_intensity`'s `≈1.4286` exactly as in the non-reasoning example.

## Data-centre overhead (PUE)

PUE = total facility energy / IT-load energy. Resolved per **deployment
profile**, because "self-hosted" spans a desk and a machine room.

| profile | PUE | setting |
|---|---|---|
| hyperscaler cloud | 1.2 | `TRET_DATACENTER_PUE` |
| workstation | 1.05 | `TRET_LOCAL_PUE` |
| on-prem facility | 1.56 | `TRET_ONPREM_PUE` |

Published figures behind those numbers:

| source | PUE | date |
|---|---|---|
| Uptime Institute survey (879 operators) | 1.56 | 2024 |
| Microsoft, FY2024 | 1.16 | 2024 |
| AWS | 1.15 | 2024 |
| Google 2025 Environmental Report | 1.09 | 2024 data |

**tret's 1.2 cloud default is mildly conservative** — above all three
hyperscaler self-reports, well below the industry average. That is the safe
direction for a facility tret cannot see. Self-reported figures are fleet
averages, not the building that served your request.

**A generic or on-prem deployment should use 1.56, not 1.2.** Set
`TRET_LOCAL_DEPLOYMENT_PROFILE=onprem_datacenter` if you self-host in a real
machine room; the workstation default (1.05) is only honest for a desktop.

A PUE below 1 is physically impossible, so a misconfigured value below 1 is
clamped to 1 rather than allowed to shrink the number.

## Grid intensity, and its basis

Default **470 gCO2e/kWh** — the IEA's 2024 global power-sector average
([Electricity 2025](https://www.iea.org/reports/electricity-2025), reported as
~460-480; 470 is the midpoint). Tret's previous 400 was stale-low and uncited.

| reference | g/kWh | note |
|---|---|---|
| IEA global, 2024 | 470 | tret default |
| EPA eGRID2023 US average | 350 | subregions span >10x |
| low-carbon grid | ~30 | e.g. Sweden |
| coal-heavy grid | ~750 | |

### Location-based vs market-based

The GHG Protocol Scope 2 Guidance requires distinguishing:

- **location-based** — the physical grid that served the load.
- **market-based** — contractual renewable claims (PPAs, RECs, GOs).

They answer different questions, they are not interchangeable, and they must
never be summed. Google's published 0.03 gCO2e/prompt is *market-based* and
roughly 3x below its own location-based figure — the same electricity, a
different accounting question.

So tret records a basis label on every run (`grid_co2e_basis`:
`location_based` | `market_based` | `unspecified`), and
`GET /api/analytics/emissions` **stops reporting a single carbon total** for a
window that mixes them (see [Basis separation](#basis-separation-what-may-be-added-to-what)).
The shipped default factor is a physical-grid average, hence `location_based`. An
operator's own factor defaults to `unspecified` until they say which it is —
tret will not guess a basis on your behalf, and a factor passed explicitly into
the accounting call is always `unspecified`.

### Per-provider factors: `TRET_GRID_FACTORS`

One global factor is the wrong shape for a real deployment. An operator may
self-host in a known place *and* call two cloud providers, one of which publishes
a factor they accept. So the grid factor is configurable **per provider**, as
JSON keyed by tret provider name (`local`, `anthropic`, `kimi`, `openrouter`):

```
TRET_GRID_FACTORS={"local":{"g_per_kwh":42,"basis":"location_based","label":"Ontario grid, IESO 2024"},"anthropic":{"g_per_kwh":120,"basis":"market_based","label":"provider PPA disclosure"}}
```

| key | required | meaning |
|---|---|---|
| `g_per_kwh` | yes | gCO2e/kWh. Must be positive and finite — a zero would claim carbon-free electricity, which no grid delivers. |
| `basis` | no | `location_based` \| `market_based` \| `unspecified`. Defaults to `unspecified`: tret does not know what your number represents and will not guess. |
| `label` | no | A short note (≤ 80 chars) shown beside the factor in the run's provenance table — where you got it, in your words. |

Validation is strict inside an entry and forgiving about provider names, and the
asymmetry is deliberate:

- An **unknown key inside an entry** is a hard startup error. A mistyped
  `gCO2e_per_kwh` that was quietly ignored would leave you believing you had
  configured a factor while tret applied the global default.
- An **unrecognised provider name** logs a warning at startup and is kept. The
  catalog gains providers over time, and refusing to boot on a config that was
  correct when it was written is the worse failure. Such an entry is inert until a
  provider of that name exists.
- A **blank** value means "not set", exactly like `TRET_LOCAL_GRID_CO2E_G_PER_KWH`
  — a `${VAR:-}` interpolation for a knob you never set must not stop the backend
  booting.

### Precedence, and what each run records

| rank | rule | source key | setting |
|---|---|---|---|
| 1 | a factor passed straight into the accounting call | `run_override` | — (no basis claimed) |
| 2 | `TRET_GRID_FACTORS` entry for the run's provider | `provider:<name>` | `TRET_GRID_FACTORS[<name>]` |
| 3 | the self-hosted factor, on a local run (**legacy**) | `local_setting` | `TRET_LOCAL_GRID_CO2E_G_PER_KWH` |
| 4 | the global default | `global_default` | `TRET_GRID_CO2E_G_PER_KWH` |

Every run records **which rule applied**, not just the number it produced:
`grid_co2e_source` carries the stable key above and `grid_co2e_label` carries your
label when you set one, and both also appear on the `grid_intensity` provenance
record as `source_key` / `source_rule` / `source_label`. A provenance table can
therefore explain *why* a factor was used, which is the more interesting half once
several factors are configured and one run looks wrong.

`TRET_LOCAL_GRID_CO2E_G_PER_KWH` and `TRET_LOCAL_GRID_CO2E_BASIS` are
**legacy**: still read, still documented, and behaving exactly as they always
have for any provider without an entry of its own. `TRET_GRID_FACTORS` with a
`"local"` key supersedes them and is strictly more expressive (it carries a
label), so prefer it in new configuration. Nothing is being removed.

### Why this is configuration and not geolocation

The obvious-looking feature here is to detect the caller's region and apply that
region's grid factor. Tret does not do this, and will not, and it is worth being
explicit because a reader will ask:

- **The caller's location is not the load's location.** For a cloud API call, the
  request is served by a data centre whose region has nothing to do with where the
  caller sits. Attributing a Toronto grid factor to inference served from Virginia
  is not an approximation; it is a different number about a different place.
- **Providers do not disclose the serving region** per request. There is nothing
  to read even if tret wanted to.
- **A router makes it worse.** OpenRouter sends a call to whichever upstream has
  capacity, so even the *provider* — let alone the region — can vary between two
  identical requests.
- **An IP lookup is also a network call and a privacy leak**, and tret's promise
  is that it makes no network calls except to the LLM providers you configure
  (plus an optional model-catalog fetch), with no telemetry ever. A geolocation
  dependency would break that for a number that would still be wrong.

Where location *is* knowable, the operator is the one who knows it: they
self-host somewhere specific, or they have pinned a provider to a region, or they
have a supplier disclosure in hand. So the factor comes from them. This adds
**zero network calls** — `TRET_GRID_FACTORS` is parsed from the environment at
startup and nothing else happens.

### Basis separation: what may be added to what

Per-provider factors make a basis-mixed window the normal case rather than an edge
case, which forces the question of what a window total actually means.

| figure | summable across bases? | why |
|---|---|---|
| energy (Wh) | **yes** | A kWh is a kWh regardless of how its carbon is accounted. |
| dollars | **yes** | A price has no Scope 2 accounting method. |
| carbon (gCO2e) | **no** | Location-based and market-based figures answer different questions; adding them yields a meaningless number, not a smaller one. |
| scope 1/2/3 | **no** | Scope totals *are* carbon, so they inherit the rule exactly. |
| the judgment band | **no** | It is a band around carbon. |

So `GET /api/analytics/emissions` reports `co2e_g`, the three scope figures,
`baseline_co2e_g`, `avoided_co2e_g`, `avoided_pct` and the band as **`null`**
whenever the window spans more than one basis, sets
`totals.carbon_is_summable: false`, and puts the real figures in **`by_basis`** —
one row per basis, each summable by construction. Energy and money stay populated.

Reporting a total with a warning beside it was the alternative, and it is worse: a
number on a page gets quoted, and the warning does not travel with it.

Two details worth stating:

- A run recorded **before tret stored a basis** counts as its own group (`null`).
  It cannot be shown to share a basis with a location-based run, and assuming it
  does would be the same error in the other direction. A window of only such runs
  has one group, so it keeps its total.
- Per-row rollups follow the same rule and mostly keep their figures: a model
  belongs to one provider, so a `by_model` row usually stays summable even when the
  window does not. A harness that ran two providers does not, and says so.

### Regional sourcing

**Tret ships no external API integration for grid intensity, deliberately.** A
live dependency in the accounting path would make a stored run's carbon figure
depend on a third party's uptime, and each of these sources carries licence or
coverage limits an operator has to accept for themselves. The seam is
configuration: `TRET_GRID_FACTORS` per provider, `TRET_GRID_CO2E_G_PER_KWH`
globally (and the legacy local variant). You paste in a figure you sourced and can
defend; tret never fetches one.

| source | granularity | catch |
|---|---|---|
| [Electricity Maps](https://www.electricitymaps.com/) | hourly, per zone | live API: free tier is one zone, non-commercial. Yearly averages are bundled — see below |
| [WattTime](https://watttime.org/) | marginal rate, sub-hourly | marginal ≠ average; a different question |
| [eGRID](https://www.epa.gov/egrid) / IEA | annual average | what most frameworks expect |

**Bundled yearly zone averages.** The one exception to "you paste a figure"
is a *static* table: tret bundles Electricity Maps' published yearly
per-zone averages (their free ODbL datasets, imported offline, never fetched
at run time) together with a map from cloud region names (`us-east-1`,
`europe-west4`, `westeurope`, …) to the zone each region's data centres sit
in. It is consulted only when a workspace has pinned a provider to a region
(`grid.regions`) and nothing they set themselves priced the provider — the
`dataset` rung, just above the shipped default — so the caveat above
still holds in full: a hosted provider's serving region is not knowable, and
pinning one remains the operator's statement, not tret's inference. How the
table is generated, its licence, and the region map are in
[grid-zones.md](grid-zones.md).

## Embodied hardware

`TRET_EMBODIED_G_PER_RUN` defaults to **0**, which means local inference is
reported with **no manufacturing carbon at all**. That is a real understatement,
and it flatters exactly the option tret's own routing prefers.

Cited constants to set it from (EcoLogits' convention, so your figure is
comparable with published ones):

| constant | value |
|---|---|
| NVIDIA H100 | 273 kgCO2eq/unit |
| server chassis, excl. GPUs | 5,700 kgCO2eq |
| hardware lifetime | 3 years |
| batch size | 64 |

`emissions.amortized_embodied_g_per_run()` does the arithmetic: a run carries its
share of the hardware, i.e. total kg / (lifetime runs x batch size). One GPU plus
a chassis over 100,000 runs at batch 64 is 0.933 g per run.

**Read this before using it.** The 273 kg GPU figure traces to Boavizta, which
states it *could not find real GPU manufacturing LCA data and assumed parity with
CPU/RAM manufacturing*, and gives 30-50% margin of error on manufacturing
footprints generally. This is a placeholder resting on a placeholder. It is
offered because 0 is worse, not because it is good. Tret marks its confidence
`placeholder` in the provenance block for exactly this reason.

## Money saved

The most defensible metric here, and worth saying why: **per-token prices are
published and exact**, so `cost.usd` and `cost.baseline_usd` are arithmetic, not
estimation. Nothing is inferred from hardware, batching or a grid mix.

```
avoided_usd     = baseline_usd - actual_usd
avoided_usd_pct = 100 * avoided_usd / baseline_usd    # null if baseline_usd <= 0
```

Signed exactly like `avoided_co2e_g`: negative means this run cost **more** than
the baseline would have — a surcharge, reported as one.

### The percentage, and why it gets a decimal place when carbon does not

`avoided_usd_pct` ("N.N% cheaper than frontier") is carried on `cost.avoided_pct`
and on `baseline.avoided_usd_pct` — same figure, two access points, so it travels
wherever `avoided_usd` already does (run summary, run detail, the SSE
`usage`/`done` events, chat messages, and the `/api/analytics/emissions` window
totals and `by_model`/`by_harness` rollups).

It is reported to **one decimal place**, unlike the carbon comparison's
deliberately coarse "~50x lighter" — and that asymmetry is intentional, not an
oversight. The carbon ratio divides two *estimated* figures, each carrying the
same order-of-magnitude judgment band, so a decimal place on it would be false
precision. The money ratio divides two *arithmetic* figures — published list
prices multiplied by exact token counts — so a decimal place on it is simply
correct.

`avoided_usd_pct` is **null, never `0%`**, in exactly three cases: no baseline
could be resolved, the baseline's own cost for these tokens is zero (a
misconfigured `TRET_EMISSIONS_BASELINE_MODEL` pointed at a free model has no
denominator to divide by), or the run predates the money comparison entirely.
It is exactly **`0.0`** only when the run genuinely used the baseline model
itself — comparing a run to itself is a real zero, not a missing one. A window
rollup follows the same rule at bucket scale: `avoided_usd_pct` there is
computed from **summed dollars** (`sum(avoided_usd) / sum(baseline_usd)`), never
by averaging each run's own percentage — averaging percentages would let a
handful of small-baseline runs swamp a window whose dollars are dominated by a
few large ones, which is not the same number and is not honest.

### List prices, not your prices

`prices_are_exact` is true of the *prices*, not of what any particular operator
actually pays. The catalog carries published per-token list rates; a negotiated
enterprise agreement, committed-spend discount, or promotional credit is not
modelled, so `avoided_usd`/`avoided_usd_pct` describe list-price API spend, not
an operator's actual invoice.

### The same-token caveat applies here too

The counterfactual behind `avoided_usd`/`avoided_usd_pct` is still an
assumption, and it is the *same* assumption the carbon comparison makes (see
[The counterfactual](#the-counterfactual) below): these are the tokens *this*
run actually produced, re-priced through the baseline model. A different model
would not have produced identical token counts — it might need more turns, or
produce a worse answer someone redoes. Money and carbon can also disagree: a
cheap reasoning model saves dollars while costing more carbon, and tret
reports both rather than picking the flattering one.

### Zero-cost (local) models: a real 100%, and a deliberate asymmetry

A self-hosted model bills **$0** through tret's token API, so it can
legitimately read `avoided_usd_pct: 100.0` — "100% cheaper than frontier." That
figure is correct as far as it goes, and it does not go very far: it is **list-
price API spend only**. It excludes the electricity the machine actually drew
and any amortized hardware cost — tret does not model self-hosting's
electricity bill or capital cost, so those are not zero, they are simply not
counted in this figure. Every run with a zero-cost model carries a named caveat
saying exactly this, `money_excludes_self_hosting_costs`, with
`direction: "overstates"` — the real economic saving is smaller than 100% once
those costs are counted, even though tret cannot say by how much.

This is a **deliberate asymmetry** with the carbon accounting above, worth
stating plainly: the emissions model *does* attribute Scope 2 electricity (and,
if `TRET_EMBODIED_G_PER_RUN` is set, embodied hardware) to a self-hosted run.
So the same run that reads "100% cheaper than frontier" in dollars can — and
typically does — carry a real, nonzero `co2e_g`. Money tracks what tret's
token API bills; carbon tracks what running the model actually draws. Neither
figure is wrong; they are answering different questions, and tret reports both
rather than letting the flattering one stand alone.

## Uncertainty: a band, not an interval

Every figure carries `co2e_g_low` / `co2e_g_high` at **central / 2.5** and
**central x 2.5**, configurable via `TRET_UNCERTAINTY_BAND_LOW` / `_HIGH`.

**This is a judgment band matching field practice. It is NOT a confidence
interval and NOT a standard deviation.** No credible methodology in this field
publishes an interval, and presenting one would be a false precision claim. The
band is flagged `is_confidence_interval: false` in the JSON so no renderer can
mislabel it by accident.

What the band is calibrated against:

| finding | source |
|---|---|
| order-of-magnitude correctness only | Green Algorithms — Lannelongue et al., *Advanced Science* 2021, [10.1002/advs.202100707](https://doi.org/10.1002/advs.202100707) |
| 30-50% margin on manufacturing | Boavizta |
| measuring tools underestimate 20-30%; spec-based estimation -40% to +40% | Fischer, [arXiv:2509.22092](https://arxiv.org/abs/2509.22092), Sept 2025 |
| 45% swing from batch-size assumption alone | Jegham et al. 2025 |

The Fischer result is the sharpest of these: CodeCarbon *actually measures
hardware* and still underestimates ground truth by 20-30%, mainly from cooling
and PSU losses invisible to software. Tret does not measure hardware at all.

### Per-factor sensitivity

`uncertainty.contributions` says what moves if one input alone is wrong. As
expected, **grid intensity and the energy class dominate**:

| factor | low | high | dominant |
|---|---|---|---|
| grid intensity | 0.06x | 1.6x | yes |
| energy class | 0.33x | 3.0x | yes |
| batching | 0.55x | 1.45x | |
| spec-estimation bias | 0.6x | 1.4x | |
| PUE | 0.91x | 1.3x | |
| output/input ratio | 0.9x | 1.15x | |
| hidden reasoning tokens | 1.0x | 1.2x (3.0x on tier R) | on tier R |
| unbatched local inference | 1.0x | 5.0x (local only) | on local |

Two notes. The last two are **one-sided** — they can only push the real figure
up, never down. And the product of these multipliers is deliberately **not** the
headline band: multiplying them gives a range far wider than any published
methodology claims, and presenting that as the answer would be its own kind of
dishonesty. The decomposition exists so an operator can see which single
refinement (almost always: a regional grid factor) is worth making.

## GHG Protocol scope mapping

Scopes are relative to a *reporting entity*. Here that entity is the **tret
operator**, not the model provider and not tret-the-project.

| scope | contents |
|---|---|
| **Scope 1** | always `0.0`, explicitly |
| **Scope 2** | electricity for self-hosted (`provider == "local"`) inference |
| **Scope 3** | all cloud inference; plus embodied hardware for local inference |

- **Scope 1 = 0** because running inference burns no fuel on the operator's
  premises. A nonzero Scope 1 could only come from on-site generation, which
  tret cannot observe and must not invent. It is reported as an explained zero
  rather than omitted: a missing scope reads as an oversight, an explained zero is
  a claim you can check.
- **Scope 2** is purchased energy. Self-hosting means the operator buys the kWh,
  so those emissions are theirs at the second scope. This is where
  `TRET_GRID_FACTORS={"local":{…}}` belongs — or the legacy
  `TRET_LOCAL_GRID_CO2E_G_PER_KWH`, which still works.
- **Scope 3** covers cloud inference as a *purchased service*: the provider's own
  Scope 1/2 becomes the operator's Scope 3 Category 1 (purchased goods and
  services). They never bought the electricity — they bought tokens. Local
  embodied hardware is Category 2 (capital goods).

So: **a cloud run is entirely Scope 3. A local run splits** — electricity into
Scope 2, embodied hardware into Scope 3. `co2e_g` is always exactly
`scope1_g + scope2_g + scope3_g`; that invariant is asserted across every
configuration in `backend/tests/test_emissions.py`.

## The counterfactual

For each run, tret re-prices **the identical token counts** through a baseline
model (by default the highest-energy-class curated non-local catalog entry, ties
broken by model id so the choice is deterministic across processes), in both
carbon and dollars.

- **Same-token, not same-task.** A different model would not produce identical
  token counts. A smaller model often needs more turns, or retries, or produces a
  worse answer someone redoes. Tokens are held fixed because that is the only
  comparison tret can make without guessing.
- **It is an efficiency indicator.** "This run was lighter than the heaviest
  option, by roughly this much, at equal token counts." That is useful for model
  selection, and it is all it is.
- **It is not an offset, not a credit, not an emissions reduction, not a saving,
  and not booked money.** No carbon was removed from anywhere. Nothing here may be
  netted against a footprint or appear in statutory reporting.
- **It is signed.** A run on something heavier or dearer than the baseline reports
  a *negative* figure. Clamping to zero would turn honest arithmetic into a
  one-way marketing number.
- **A run on the baseline model itself reports exactly 0**, because comparing a
  run to itself contains no counterfactual.
- **An unresolvable baseline reports `null`, not 0.** If the configured baseline
  names a model the catalog does not have, tret reports no comparison rather than
  silently substituting one.
- **The comparison can cross a basis, and says when it does.** The counterfactual
  is priced at the factor the *baseline model's* provider carries, which is honest
  per side but means a run on a location-based factor can be compared against a
  market-based baseline. That difference is not a GHG Protocol quantity, so such a
  run carries the `baseline_crosses_grid_basis` caveat and the baseline block
  records its own `grid_co2e_basis` and `grid_co2e_source`. Configure one basis
  across your providers if you need the comparison to be like for like.

## Exclusions, stated plainly

### Training is not allocated

Published per-query training amortizations span **~0.0001 to ~1.8 gCO2e/query** —
four orders of magnitude — and the spread comes almost entirely from the assumed
number of queries a model serves over its life, which providers do not disclose.
A number whose value is set by an unobservable free parameter is not an estimate.

To include it you would need the provider's total training energy, its grid mix
at training time, and a defensible lifetime query count. Tret has none of the
three, so it reports training as an explicit exclusion with a value of `0.0` and
confidence `excluded`, rather than picking a point in a 10,000x range.

### Cloud embodied hardware is not counted

It sits inside the purchased service (Category 1) and tret has no basis for
splitting it out, so a cloud run's Scope 3 is electricity-derived only. This
understates it.

### Reasoning tokens may not be in the counted output

Tret's token counts come from **provider usage reporting**. For several providers
hidden reasoning tokens are **not included in the billed output count**. Two
consequences, both named rather than absorbed:

1. Real generation work — and therefore energy — is **higher than counted** for
   those models. The bias is one-sided.
2. Energy per *visible* output token is **inflated** for these models, which is
   part of why the fitted `b` for o3 and DeepSeek-R1 is so much larger than for
   non-reasoning models. Some of that figure is work tret cannot see attributed
   to tokens it can.

This is recorded as the `reasoning_token_accounting` caveat on every run, and
strengthened on tier-R runs.

### Local inference is not batched

The class constants come from batched serving stacks (the reference data infers
batch sizes in the dozens; EcoLogits defaults to 64). A single-user local model
carries the whole accelerator for one request, so its real per-token energy can be
several times class S. Combined with embodied hardware defaulting to 0, **local
runs are the roughest estimate tret produces**, and both biases understate them.

### Everything else around inference

Excluded: water, network transfer, storage, retrieval and embedding calls, and the
router's own model call — the same scope as the dollar cost tret already reports.

## External anchors

Where tret's output sits against published per-prompt figures. These cover one
model each on one operator's stack, so they bound the order of magnitude rather
than validate the ladder.

| source | figure | scope |
|---|---|---|
| Google, median Gemini text prompt ([arXiv:2508.15734](https://arxiv.org/abs/2508.15734), 21 Aug 2025) | 0.24 Wh, 0.03 gCO2e, 0.26 mL water | accelerator 58%, host CPU/DRAM 25%, idle/reserve 10%, DC overhead 8%; excludes embodied; **market-based** carbon |
| Mistral, 400-token Le Chat response (22 Jul 2025) | 1.14 gCO2e, 45 mL water | ISO 14040/44 + GHG Protocol Product Standard, reviewed by Carbone 4 / ADEME; **includes** training amortization and embodied |
| Anthropic | *nothing published* | — |

Reading these honestly:

- Against Google, a Flash-class model at class M puts a 1k-in/300-out prompt at
  ~0.42 Wh compute (~0.5 Wh with PUE) — roughly **2x** Google's figure for their
  own stack. Same order, on the conservative side. Note Google's number is
  market-based carbon, so its 0.03 gCO2e is not comparable with tret's
  location-based figure at all.
- Against Mistral, tret's figure for a comparable response is *lower*, and the
  reason is scope: Mistral's 1.14 gCO2e includes training amortization and
  embodied hardware, both of which tret excludes.
- **Anthropic publishes nothing.** This matters more than the other two rows,
  because tret's default models are Anthropic's — the models tret is most likely
  to be running are the ones with the least public data behind their energy class.

## Replacing a default with your own factor

Every default is replaceable, and each one is worth a different amount. In rough
order of payoff:

### 1. Grid intensity (biggest single win)

Per provider, which is the shape a real deployment has:

```
TRET_GRID_FACTORS={"local":{"g_per_kwh":42,"basis":"location_based","label":"Ontario grid, IESO 2024"},"anthropic":{"g_per_kwh":120,"basis":"market_based","label":"provider PPA disclosure"}}
```

Or globally, which is still the fallback for every provider without an entry:

```
TRET_GRID_CO2E_G_PER_KWH=<your region or supplier>
TRET_GRID_CO2E_BASIS=location_based|market_based
```

The legacy self-hosted pair still works and is not going away:

```
TRET_LOCAL_GRID_CO2E_G_PER_KWH=<your site factor>
TRET_LOCAL_GRID_CO2E_BASIS=market_based
```

Get the numbers from eGRID (US subregional), your national inventory, your
supplier's disclosure, a provider's own published factor, or Electricity Maps /
WattTime if you accept their terms. **Say which basis each one is** — a
market-based figure mixed into a location-based total is not a smaller number, it
is a meaningless one, and tret will withhold the combined total rather than print
it (see [Basis separation](#basis-separation-what-may-be-added-to-what)).

A worked example. You self-host on an Ontario grid you have a published factor
for, and you also call Anthropic, whose PPA disclosure you accept:

```
TRET_GRID_FACTORS={"local":{"g_per_kwh":42,"basis":"location_based","label":"Ontario grid, IESO 2024"},"anthropic":{"g_per_kwh":120,"basis":"market_based","label":"provider PPA disclosure"}}
```

A local run then records `grid_co2e_g_per_kwh: 42`, `grid_co2e_basis:
location_based`, `grid_co2e_source: provider:local`, `grid_co2e_label: "Ontario
grid, IESO 2024"`; an Anthropic run records 120 / `market_based` /
`provider:anthropic`. A window containing both has an energy total and a dollar
total but **no carbon total** — it has two, one per basis, in `by_basis`. That is
not tret being awkward; it is the GHG Protocol, and it is the reason the label
travels with every number.

### 2. Energy class per model

Meter your own deployment and put the result in `models.yaml` as
`energy_wh_per_mtok` (Wh per million output-equivalent tokens) for the models you
actually run. An explicit value always beats the class ladder. For open models,
Hugging Face's AI Energy Score is measured on controlled hardware and is better
evidence than tret's bucket.

If you cannot meter, at minimum review the class assignments: every one carries a
rationale comment in `models.yaml`, and the borderline calls (is this model
reasoning-tier or not?) move the figure by ~8x.

### 3. PUE

```
TRET_DATACENTER_PUE=<your provider's disclosed figure>
TRET_LOCAL_DEPLOYMENT_PROFILE=onprem_datacenter
TRET_ONPREM_PUE=<your facility's measured PUE>
```
If you self-host anywhere other than a desk, switch the profile. If you have a
metered facility PUE, use it — it is one of the few inputs here you can actually
observe.

### 4. Embodied hardware

```
TRET_EMBODIED_G_PER_RUN=<total embodied kg * 1000 / (lifetime runs * batch)>
```
Use your hardware's own published embodied footprint if the vendor discloses one.
Falling back to the H100/chassis constants above is defensible only with the
Boavizta caveat attached.

### 5. Uncertainty band

```
TRET_UNCERTAINTY_BAND_LOW=2.5
TRET_UNCERTAINTY_BAND_HIGH=2.5
```
Narrow it only if you have replaced the factors that justify its width — chiefly
metered energy and a regional grid factor. Widen it if you are running models
whose class you had to guess. Never relabel it as a confidence interval.

### 6. The baseline model

```
TRET_EMISSIONS_BASELINE_MODEL=anthropic/claude-fable-5
```
Pick the model you would otherwise have used, if the auto-selected heaviest
catalog entry is not that.

## Configuration layers

Everything above is a single `TRET_*` setting, read by whichever process runs
`energy_accounting()`. That is enough for a self-hosted, single-operator
deployment. It is not enough for a hosted one: an admin console that lets a
customer set their own grid factor cannot restart the process per request, and
a managed layer (the hosting product itself) needs to set a floor or a default
without a customer's own workspace override silently losing to it.

`tret/services/emission_factors.py` generalises the grid factor's existing
precedence (`run_override` beats `TRET_GRID_FACTORS` beats
`TRET_LOCAL_GRID_CO2E_G_PER_KWH` beats `TRET_GRID_CO2E_G_PER_KWH`, described
above under [Grid intensity, and its basis](#grid-intensity-and-its-basis)) to
every constant this document has described — PUE, embodied hardware, the
uncertainty band and the baseline model — and adds rungs above the process
environment:

```
run_override  >  harness  >  workspace  >  managed  >  env  >  dataset  >  global_default
```

* **`run_override`** — a value handed straight to one accounting call. This is
  what `energy_accounting`'s own `grid_g_per_kwh` argument has always meant;
  the other factors now accept the same kind of one-off override.
* **`harness`** — reserved for a future per-harness override. The ladder
  resolves it today; nothing populates it yet.
* **`workspace`** — an operator's own configuration for one workspace, stored
  wherever the API that manages it decides to store it. This module validates
  the document; it does not read or write a database row.
* **`managed`** — a configuration a hosting extension supplies (tret_cloud's
  admin console, for one). Recorded on the run as `managed:<name>` — a short
  name the document itself carries — so two different managed layers are
  never confused for one on a stored run.
* **`env`** — a `TRET_*` setting the operator actually set, in a real
  environment variable or a loaded `.env` file. Detected through Pydantic's
  `model_fields_set`, not by comparing against the shipped default: an
  operator who deliberately sets `TRET_GRID_CO2E_G_PER_KWH` back to `470` is
  still recorded as `env`, not `global_default`.
* **`dataset`** — the grid factor only. When a workspace has pinned the run's
  provider to a region and *nothing an operator set* priced that provider —
  no document above, no `TRET_GRID_FACTORS` entry, no legacy local setting,
  no explicitly set global factor — the bundled table of published yearly
  zone averages (Electricity Maps, ODbL — see [grid-zones.md](grid-zones.md))
  supplies the figure, recorded as `dataset:zone:<zone>`. It displaces only
  the shipped default: a region pin says where the load ran, not that the
  operator's own figure or its GHG Protocol basis should be discarded.
  Reached only *because* an operator pinned a region; nothing infers one,
  and an unpinned provider skips this rung.
* **`global_default`** — the shipped constant, when nothing above chose
  otherwise. Everything in this document up to this section describes exactly
  this rung.

**Resolution is per factor, not per document.** A workspace override document
that only sets its grid factor still takes its PUE, embodied figure,
uncertainty band and baseline model from whichever layer below it is the most
specific one that set them — `managed`, then `env`, then the shipped default.
Setting one thing does not silently freeze everything else at that layer.

**Within one document, a per-provider entry beats that document's own
default** — `grid.providers.anthropic` over `grid.default`, exactly like
`TRET_GRID_FACTORS` over `TRET_GRID_CO2E_G_PER_KWH` today. **Across documents,
a more specific layer's default beats a less specific layer's per-provider
entry** — a workspace's flat `grid.default` outranks a managed layer's
`grid.providers.anthropic`, because the workspace is the operator's own word
for *this* workspace, and a managed layer's provider-specific guess is still a
guess made for someone else.

An override document (workspace, managed, or the reserved harness layer) is
the shape:

```json
{
  "version": 1,
  "grid": {
    "default": {"g_per_kwh": 42, "basis": "location_based",
                 "label": "Ontario grid, IESO 2024"},
    "providers": {"anthropic": {"g_per_kwh": 120, "basis": "market_based",
                                 "label": "provider PPA disclosure"}}
  },
  "pue": {"cloud": 1.2, "local_profile": "onprem_datacenter", "local": 1.38,
          "label": "Site metered, Q2 2025"},
  "embodied": {"g_per_run": 0.31, "label": "vendor disclosure"},
  "band": {"low": 2.0, "high": 2.0, "label": "narrowed after metering"},
  "baseline_model": "anthropic/claude-opus-5",
  "source_name": "acme-admin"
}
```

Every key is optional. **Any block that sets a number requires a label** — the
same rule `GridFactor` already enforces for `TRET_GRID_FACTORS`, extended
everywhere: a number with nowhere to say where it came from is worse than no
override at all. `baseline_model` is recorded as a plain string here and is
**not** validated against the model catalog by this module — that stays the
API's job, exactly as it already is for `TRET_EMISSIONS_BASELINE_MODEL`.

**Source, layer and setting strings.** Every resolved factor on a run carries
three related but distinct fields, and the grid factor's `grid_co2e_source` /
`grid_co2e_layer` are the ones that were already public before this section
existed:

- `source` (`grid_co2e_source` for the grid factor) — the precise,
  human-legible key: `run_override`, `provider:<name>` / `local_setting` /
  `global_default` (unchanged from before this section, for the env/global
  rungs — see [Grid intensity, and its basis](#grid-intensity-and-its-basis)),
  or, once a configured layer wins, `harness` / `harness:provider:<name>` /
  `workspace` / `workspace:provider:<name>` / `managed:<name>` /
  `managed:<name>:provider:<name>`.
- `layer` (`grid_co2e_layer` for the grid factor) — one of the six rung names
  above. Coarser than `source`: every provider-specific win at a layer folds
  into that layer's name.
- `setting` — the one path that actually applied, never a list of the paths
  that might have: a `TRET_*` env var name for the `env` / `global_default`
  rungs (unchanged strings — a run recorded before this section existed reads
  identically), or a dotted override path (`workspace.emissions.grid.providers.anthropic`,
  `managed.emissions.pue.local`) otherwise.

Every record in a run's `factors` list carries its own `layer` — the constants
that are not layered (the energy class, the token weights, per-token prices)
report `global_default`, since nothing above resolves them any differently.
The run also carries a top-level `factor_layers`: every layer that contributed
*anything*, most specific first — `["global_default"]` when nothing was ever
configured, `["workspace", "env"]` when a workspace overrode the grid factor
and everything else fell through to the environment.

**Stored runs snapshot the factors in force. There is no backfill.** A run
persists the `FactorSet` that was actually resolved for it at the moment it
ran; changing a workspace's grid factor tomorrow does not, and must not,
rewrite what a run from yesterday says it used — the same principle
[Window rollups are as-recorded](#window-rollups-are-as-recorded) already
states for the plain `TRET_*` settings, now extended to every layer above
them.

### Effective factors and the settings API

The `workspace` layer above is managed through three routes, all under
`GET/PUT/DELETE /api/workspace/settings/emissions`:

- **`GET`** — open to any workspace member. Returns three things at once:
  `overrides` (the stored document verbatim, `{}` if none), `effective` (what
  the *next* run on each of `local` / `anthropic` / `kimi` / `openrouter`
  would resolve to, as a `Resolved` value per factor — value, layer, source,
  label, url, as_of, setting), and `shipped_defaults` (tret's own numeric
  defaults with their citation, so a settings form can show what an empty
  field falls back to without reading this document). **Fails open** when the
  stored document no longer validates against the current schema (a
  downgrade, a hand-edited row, a field a later version removed): `overrides`
  still carries the raw document (so the panel can show it and offer "clear
  overrides"), `effective` is `null` rather than guessed, and `error` names
  the validation failure as the same plain dotted-field string PUT's 422 uses.
  PUT and DELETE are unaffected — writing (or clearing) the document always
  leaves it in a state that validates.
- **`PUT`** — admin or owner only. The body is validated as the same
  `EmissionsOverrides` document described above, so a request that violates
  the label rule 422s with the offending validator's own message — a plain
  string naming the dotted field (`"grid.default.label is required when
  grid.default.g_per_kwh is set"`), never a nested per-field error blob. A
  `baseline_model` that does not name a model in the catalog, or names a
  local one, 422s the same way. Past validation, a registered workspace gate
  (`check_workspace_gate(db, workspace_id, "emissions_factors_edit")` —
  `tret/engine/extensions.py`) gets a veto before anything is written; a
  refusal is a 403 carrying the gate's own `reason`/`detail`, identical to the
  invite-creation gate `api/workspaces.py` already has. A stored write is
  stamped with `updated_by` (the caller's email) and `updated_at` (ISO UTC)
  and replaces `Workspace.settings["emissions"]` wholesale, leaving every
  other key in `settings` untouched.
- **`DELETE`** — same admin-or-owner requirement and the same gate action
  (clearing a workspace's overrides is as much an edit as setting them);
  removes the key and returns the same shape `GET` does, with `overrides` back
  to `{}`.

Past the gate, every successful PUT and DELETE also notifies
`run_workspace_settings_hooks` (`tret/engine/extensions.py`) with the
workspace id, `"emissions"`, the document as it was stored immediately
before the change (`None` on a first write) and immediately after (`None` on
a DELETE), and the caller's user id — the same fail-open extension seam a
post-run hook uses, so an extension may observe every change without the
open-source engine knowing or caring what it does with it. tret_cloud
registers one of these to keep a change history; core itself keeps only the
current document plus its own `updated_by`/`updated_at`, and a broken or
raising hook can never turn a successful write into an error response.

**The runner snapshots the factor set at run start, not per call.**
`HarnessEngine.execute()` loads a run's workspace document and any managed
layer once, as soon as the run's workspace is known, and turns each into a
`FactorSet` per provider (and per model — a `model_overrides` entry is
resolved per model id, not per provider) as each model segment begins (a run's
`model_timeline` can span more than one provider, and each gets its own
layered resolution). The router's own model-choice call and the compaction
summarizer's call are accounted under the same loaded layers, via the
identical `factors=` parameter `energy_accounting` and `overhead_call` already
take. Loading either document is never allowed to fail a run: a workspace row
that no longer validates, or a managed-layer extension that raises, falls back
to `factors=None` — today's behaviour, resolving straight from `Settings` —
logged, not raised. The loaded documents (and the `FactorSet`s already
resolved from them this run) live on a per-execution object threaded
explicitly through the call graph, never on the `HarnessEngine` instance
itself — that engine is process-wide and serves runs from every workspace
concurrently, so per-run configuration held on `self` would eventually leak
from one run's workspace into another's persisted accounting.

### `POST /api/analytics/emissions/whatif`

A read-only, on-the-fly recompute of a recent window's emissions under a
*scenario* factors document — "what would this window's carbon and energy have
looked like under these settings instead?" Never writes anything: no run row,
no workspace document, is touched by this endpoint.

**Request**: `{"project_id": <uuid or null>, "days": <1-3650, default 30>,
"factors": <a partial EmissionsOverrides document>}`. `factors` is validated
the same way a `PUT` to the settings API is — a violation 422s with the same
plain, dotted-field message (`_validation_detail`, shared by both routers so
they can never disagree about how a validation error reads) — and the
`emissions_whatif` workspace gate may refuse the call outright (403, the
gate's own `reason`/`detail`), the same fail-open extension seam every other
gated write in this API uses.

**Response**: `recorded` (the window's stored, as-recorded rollup — identical
to what `GET /emissions` returns) and `scenario` (the same rollup, recomputed
per run against `factors`), plus `delta` (`co2e_g`, `co2e_pct` — an **integer**,
by the same deliberate carbon-percentage-stays-coarse rule as everywhere else
in this doc, unlike the one-decimal money percentages — `energy_wh`,
`avoided_usd`; the carbon fields are `null` under the identical cross-basis
rule `by_basis` uses elsewhere), `runs_recomputed`, `runs_skipped`, `basis` (a
one-line explanation of what changed and why), and an optional `warnings` list
— present only when something had to be excluded from the scenario, e.g. the
workspace's own stored override document no longer validates (treated as no
workspace layer for this recompute, same as a run's own fail-open contract
above, with a warning naming why rather than a silent difference from what the
workspace normally configures).

**Layering semantics — the `layer_note`.** The scenario's `factors` document is
passed as `build_factor_set`'s `harness_settings` — the reserved,
more-specific-than-workspace rung nothing else populates today — layered on
top of whatever the workspace and any managed layer already configure. That
means a scenario need not repeat factors it isn't changing: leaving `grid`
unset in the request still resolves the workspace's own configured grid
factor (or the environment, or the shipped default) underneath it. It also
means the recomputed block's `grid_co2e_layer`/`factor_layers` reads
`"harness"` for anything the scenario document itself set — the response's
`scenario.layer_note` says plainly that this is the one-off scenario document,
not a saved per-harness override (nothing else ever writes that layer).

**The catalog-miss exclusion rule.** A run whose model (or, for a
`model_timeline` run, any segment's model) is no longer in the catalog cannot
be recomputed — there is no `ModelInfo` left to price it against. Such a run
is excluded from **both** `recorded` and `scenario`, never just one: the two
sides must cover the identical population of runs, so `runs_skipped` moves the
same rows out of both totals, and `basis` states the exclusion and the count
verbatim. A `model_timeline` run is mirrored segment by segment — one
`energy_accounting` call per segment against that segment's own model and
provider factor set, combined with `combine_accountings` — exactly how the
stored block was produced in the first place, never one call over the run's
running totals against whichever model happened to be current.

**Read-only guarantee.** No `db.add`/`commit`/`flush` anywhere in this
endpoint's call graph — every number is computed in Python from rows already
in the database and the request's own `factors`. Verified by
`tests/test_emissions_whatif.py`'s `test_recompute_never_writes_to_the_run`,
which snapshots a run before and after a scenario call and asserts they are
byte-for-byte identical.

**This endpoint recomputes estimates, never a measurement.** A self-hosted run
can now genuinely carry `energy_source: "measured"` — see [How a measurement
reaches a run](#how-a-measurement-reaches-a-run) above for the two paths
(`TRET_LOCAL_ENERGY_METER=nvidia_smi`, or an external reading through the SDK
or `tret run --measured-wh`) — but this what-if endpoint still only ever
recomputes the *estimate* a run's tokens would produce under different
factors. A run whose stored `energy_accounting["energy_source"]` is
`"measured"` or `"mixed"` is recomputed the same as any other: `scenario`
reprices its token counts through the requested factors exactly as `recorded`
reflects what was actually persisted, so the two blocks answer different
questions for such a run (what was actually measured, vs. what the estimate
alone would have said) rather than the same question under two configurations.
Nothing here recomputes a meter reading, because there is nothing to
recompute it from — the Wh figure a meter or an external reading produced is
not a function of this endpoint's `factors` input at all.

## Where the numbers live in the API

Each run's `energy_accounting` block carries, additively:

- the original keys, unchanged in name and meaning: `energy_wh` (compute only),
  `energy_wh_per_mtok`, `weighted_tokens`, `grid_co2e_g_per_kwh`, `co2e_g` (the
  run total, always `scope1 + scope2 + scope3`), `pue`, `energy_wh_total`,
  `deployment`, `embodied_g`, `scopes`, `baseline`;
- the split: `input_weight`, `output_weight`, `energy_wh_per_mtok_input`,
  `energy_wh_per_mtok_output`, `tokens`, `energy_wh_by_bucket`;
- resolution: `pue_profile`, `grid_co2e_basis`, `reasoning_tier`, plus
  `grid_co2e_source` (which precedence rule chose the grid factor:
  `provider:<name>` | `local_setting` | `global_default` | `run_override`, or
  — once a workspace/managed/harness layer is in play, see
  [Configuration layers](#configuration-layers) — `workspace` |
  `workspace:provider:<name>` | `managed:<name>` |
  `managed:<name>:provider:<name>` | `harness` | `harness:provider:<name>` |
  `dataset:zone:<zone>`),
  `grid_co2e_layer` (which rung of the ladder chose it — one of
  `run_override`, `harness`, `workspace`, `managed`, `dataset`, `env`,
  `global_default`),
  and `grid_co2e_label` (the operator's own note about it). A run recorded
  before these existed carries none of the three — read them as unknown, never
  as `global_default`;
- `cost` — money against the same-token baseline, including `avoided_pct` (the
  share of frontier spend avoided, one decimal place, null rather than `0%`
  when there is no baseline or the baseline itself costs nothing) — also
  reachable as `baseline.avoided_usd_pct`, the same figure;
- `uncertainty` — the band and its per-factor sensitivity;
- `factors` — **a list** (not an object: JSONB does not preserve key order) with
  one record per constant, each carrying `value`, `unit`, `source`, `url`, `date`,
  `confidence`, a `layer` (see [Configuration layers](#configuration-layers))
  and a `setting` to change it. Confidence is one of `exact`,
  `structural`, `calibrated`, `low`, `placeholder`, `excluded`. The
  `grid_intensity` record additionally carries `basis`, `overridden`,
  `source_key`, `source_rule` and `source_label`, and its `setting` names the one
  setting that actually applied rather than the three that might have;
- `factor_layers` — every configuration layer that contributed anything to this
  run, most specific first (`["global_default"]` when nothing was ever
  configured);
- `caveats` — the named biases that apply to this particular run, each with a
  `direction` (`understates` / `overstates` / `either`). Includes
  `money_excludes_self_hosting_costs` (`direction: "overstates"`) on any run
  whose model billed nothing through the token API — see
  [Zero-cost (local) models](#zero-cost-local-models-a-real-100-and-a-deliberate-asymmetry)
  above.

`avoided_usd_pct` travels the same additive path as `avoided_usd` outside this
block too: a run summary, a run detail, the SSE `usage`/`done` events, and a
chat message all carry it, and `GET /api/analytics/emissions` carries it on the
window totals and on each `by_model`/`by_harness` row.

Nothing downstream needs to hardcode a source string or a constant: every number
in the block explains itself.

## Window rollups are as-recorded

`GET /api/analytics/emissions` sums each run's **stored** figures, frozen at the
factors in force when that run happened. It does not recompute anything at
current settings.

This is a deliberate correction of an earlier flaw: recomputing a window at
today's grid factor means correcting a setting silently rewrites history, and
makes the aggregate disagree with the per-run figures the runs API and deliverable
provenance already show. So:

- `totals` are plain sums of stored per-run values, including `avoided_usd` and
  the band (`co2e_g_low` / `co2e_g_high`). `totals.avoided_usd_pct`, and the same
  field on each `by_model`/`by_harness` row, is computed from those **summed
  dollars** (`avoided_usd` over the also-summed `baseline_usd`) — never from
  averaging each run's own percentage, which would let a handful of
  small-baseline runs outweigh a window whose spend is actually dominated by a
  few large ones. It is `null`, not `0%`, wherever a bucket has no baseline
  spend to divide by — including a window of entirely zero-cost local runs.
- Summing a band low-with-low assumes the factors are wrong in the *same*
  direction for every run — the honest assumption, since it is the same class
  table, PUE and grid factor being applied throughout.
- `factors` reports **current settings, for reference only** — nothing in the
  response was computed from them — plus `factors.recorded`, the actual
  `(deployment, grid intensity, PUE, grid basis)` combinations the window
  contains.
- `factors.mixed_factors` is `true` when the window contains more than one such
  combination, and the disclaimer says so. A window mixing a corrected grid
  factor, or cloud with self-hosted runs, or **location-based with market-based
  factors**, has no single honest factor behind its total — and the last of those
  is not summable under the GHG Protocol at all.
- `factors.mixed_grid_bases` is the stronger, separate flag for exactly that last
  case, and it is a different claim: `mixed_factors` means "no single factor sits
  behind these totals"; `mixed_grid_bases` means "there is no total". When it is
  true, `totals.co2e_g`, the three scope figures, `baseline_co2e_g`,
  `avoided_co2e_g`, `avoided_pct` and the band are `null`,
  `totals.carbon_is_summable` is `false`, `totals.not_summable_note` says why, and
  the subtotals are in **`by_basis`** — one row per basis (`location_based`,
  `market_based`, `unspecified`, then `null` for runs that recorded none), each
  with its own runs count, energy, carbon, scope split and baseline comparison.
  `totals.energy_wh`, `energy_wh_compute`, `avoided_usd` and `baseline_usd` are
  unaffected: those sum across bases legitimately.
- Every `by_model` / `by_harness` / `by_day` row carries its own `grid_bases` and
  `carbon_is_summable`, and withholds its carbon on the same rule. A `by_model` row
  usually keeps its figure — a model belongs to one provider — which is what makes
  a basis-mixed window still readable. Rows are ordered by summed carbon even
  where that sum is not published: an ordering is not a claim.
- `factors.grid_factors` reports the per-provider overrides configured **right
  now**, with the same reference-only status as the rest of that block, and
  `factors.recorded` gains `grid_co2e_source` and `grid_co2e_label` so a mixed
  window shows which rule produced each combination rather than only its value.
- Runs with **no** estimate are excluded from every total and counted in
  `totals.runs_without_estimate`. `null` is not `0`.
- Runs recorded before the scope split, the baseline, money or the band existed
  keep their `co2e_g` and are counted in `runs_without_scope_split` /
  `runs_without_baseline` / `runs_without_money_comparison` /
  `runs_without_uncertainty_band`. Their scopes are **not** back-filled.
- The scan is **bounded to the most recent 2000 runs in the window**
  (`EMISSIONS_RUN_SCAN_LIMIT`), because as-recorded rollups read each run's JSON
  block in Python rather than using JSON predicates only Postgres would honour.
  The bound, rows scanned, and truncation are reported in `scan`.

The older `GET /api/analytics/guardrails` energy block is unchanged and is *not*
this: it sums the `energy_wh` column (compute only, no PUE, no embodied, no
scopes) and derives carbon at today's factor. Expect its carbon figure to be
lower.

## Limitations — read before quoting any number

- **Not audit-grade.** Nothing here is metered, verified, or assured. It is a
  model of a model, calibrated against five models' inferred hardware.
- **Not an offset and not a reduction claim.** `avoided_co2e_g` and `avoided_usd`
  are same-token counterfactuals. Nothing tret reports removes carbon from the
  atmosphere or may be netted against anything.
- **Not for statutory or regulatory reporting** — not CSRD, not SEC climate rules,
  not GHG Protocol inventory submission — unless you first replace every default
  with metered energy for your own deployment and supplier- or region-specific
  grid factors, and can defend the energy classes for the models you actually
  used.
- **The band is a judgment, not a statistic.** Do not report it as a confidence
  interval, and do not narrow it because a stakeholder finds it uncomfortable.
- **Token-based estimation ignores a great deal**: batching and concurrency,
  hardware generation, accelerator utilisation, idle draw between requests,
  speculative decoding, model parallelism, cooling water, network transfer, and
  storage.
- **Class assignment is judgement**, and for closed models price stands in for
  size because active parameter counts are unpublished.
- **XL rests on interpolation, R rests on one well-behaved fit, and two of five
  fits were degenerate.** Those are the three places this model is thinnest.

If you need defensible numbers: meter your own deployment, put the result in
`energy_wh_per_mtok` for the models you run, set a supplier-specific grid factor
with its basis, set `embodied_g_per_run` from your own hardware, switch the PUE
profile to match where you actually run, and treat everything tret produces as a
starting sanity check rather than a result.
