# Emissions methodology

How bench turns token counts into an energy, carbon and money figure; where every
constant came from; and — more important — what the resulting numbers are *not*
good for.

Implementation: `backend/bench/services/emissions.py`. Settings:
`backend/bench/config.py`. Per-model classes: `backend/bench/providers/models.yaml`.
The routing side of the energy model is in [eco-accounting.md](eco-accounting.md);
this page is authoritative wherever the two overlap.

**One-line summary: these are calibrated estimates for comparing model choices.
They are not measurements, not an inventory, and not reportable.**

## Read this first

Bench's flagship domain is climate risk, which is exactly why this page leads
with limits rather than headline numbers. A platform that produces TCFD sections
would not accept an unfalsifiable carbon claim from a portfolio company, so it
must not make one about itself.

Two things changed from bench's first version of this model, and both matter:

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

Bench used to weight an input token and an output token equally. That is wrong,
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
degenerate. Bench records this as the `prompt_shape_residual` caveat on every
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
3.5x bench's old XL ceiling of 3,000, and 20,850 once input and output are
weighted apart. A reasoning model is not "a large model, a bit more"; it is a
different order of magnitude.

Reasoning is assigned from what a model *does*, never from what it costs. In the
reference dataset DeepSeek-R1 is among the two heaviest models measured **and**
among the cheapest models on the market. Price is not a proxy for this in either
direction, which is why bench's cheapest curated model (DeepSeek V4 Pro) carries
its heaviest energy class.

### The documented upgrade path

EcoLogits models per-token energy from **active parameter count** instead of a
class ladder, with published fitted constants α=1.17e-6, β=-1.12e-2, γ=4.05e-5 —
linear in active params, exponential decay in batch size, default batch 64.

Bench does not implement it, because it would be fed a guessed parameter count
for every closed model in the catalog, and a precise-looking function over a
guessed input is worse than an openly coarse bucket. The seam is ready:
`emissions.wh_per_mtok_for_model` is the single place every caller resolves a
model's constant, so adding an `active_params_b` to `models.yaml` and teaching
that one function to prefer it is the whole change.

Better data is also coming for open models: Hugging Face's AI Energy Score
measures ~166 models on identical H100s, and the ML.ENERGY benchmark
([arXiv:2505.06371](https://arxiv.org/abs/2505.06371)) covers open weights. Both
are stronger evidence than this ladder for the models they cover.

## Data-centre overhead (PUE)

PUE = total facility energy / IT-load energy. Resolved per **deployment
profile**, because "self-hosted" spans a desk and a machine room.

| profile | PUE | setting |
|---|---|---|
| hyperscaler cloud | 1.2 | `BENCH_DATACENTER_PUE` |
| workstation | 1.05 | `BENCH_LOCAL_PUE` |
| on-prem facility | 1.56 | `BENCH_ONPREM_PUE` |

Published figures behind those numbers:

| source | PUE | date |
|---|---|---|
| Uptime Institute survey (879 operators) | 1.56 | 2024 |
| Microsoft, FY2024 | 1.16 | 2024 |
| AWS | 1.15 | 2024 |
| Google 2025 Environmental Report | 1.09 | 2024 data |

**bench's 1.2 cloud default is mildly conservative** — above all three
hyperscaler self-reports, well below the industry average. That is the safe
direction for a facility bench cannot see. Self-reported figures are fleet
averages, not the building that served your request.

**A generic or on-prem deployment should use 1.56, not 1.2.** Set
`BENCH_LOCAL_DEPLOYMENT_PROFILE=onprem_datacenter` if you self-host in a real
machine room; the workstation default (1.05) is only honest for a desktop.

A PUE below 1 is physically impossible, so a misconfigured value below 1 is
clamped to 1 rather than allowed to shrink the number.

## Grid intensity, and its basis

Default **470 gCO2e/kWh** — the IEA's 2024 global power-sector average
([Electricity 2025](https://www.iea.org/reports/electricity-2025), reported as
~460-480; 470 is the midpoint). Bench's previous 400 was stale-low and uncited.

| reference | g/kWh | note |
|---|---|---|
| IEA global, 2024 | 470 | bench default |
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

So bench records a basis label on every run (`grid_co2e_basis`:
`location_based` | `market_based` | `unspecified`), and
`GET /api/analytics/emissions` **stops reporting a single carbon total** for a
window that mixes them (see [Basis separation](#basis-separation-what-may-be-added-to-what)).
The shipped default factor is a physical-grid average, hence `location_based`. An
operator's own factor defaults to `unspecified` until they say which it is —
bench will not guess a basis on your behalf, and a factor passed explicitly into
the accounting call is always `unspecified`.

### Per-provider factors: `BENCH_GRID_FACTORS`

One global factor is the wrong shape for a real deployment. An operator may
self-host in a known place *and* call two cloud providers, one of which publishes
a factor they accept. So the grid factor is configurable **per provider**, as
JSON keyed by bench provider name (`local`, `anthropic`, `kimi`, `openrouter`):

```
BENCH_GRID_FACTORS={"local":{"g_per_kwh":42,"basis":"location_based","label":"Ontario grid, IESO 2024"},"anthropic":{"g_per_kwh":120,"basis":"market_based","label":"provider PPA disclosure"}}
```

| key | required | meaning |
|---|---|---|
| `g_per_kwh` | yes | gCO2e/kWh. Must be positive and finite — a zero would claim carbon-free electricity, which no grid delivers. |
| `basis` | no | `location_based` \| `market_based` \| `unspecified`. Defaults to `unspecified`: bench does not know what your number represents and will not guess. |
| `label` | no | A short note (≤ 80 chars) shown beside the factor in the run's provenance table — where you got it, in your words. |

Validation is strict inside an entry and forgiving about provider names, and the
asymmetry is deliberate:

- An **unknown key inside an entry** is a hard startup error. A mistyped
  `gCO2e_per_kwh` that was quietly ignored would leave you believing you had
  configured a factor while bench applied the global default.
- An **unrecognised provider name** logs a warning at startup and is kept. The
  catalog gains providers over time, and refusing to boot on a config that was
  correct when it was written is the worse failure. Such an entry is inert until a
  provider of that name exists.
- A **blank** value means "not set", exactly like `BENCH_LOCAL_GRID_CO2E_G_PER_KWH`
  — a `${VAR:-}` interpolation for a knob you never set must not stop the backend
  booting.

### Precedence, and what each run records

| rank | rule | source key | setting |
|---|---|---|---|
| 1 | a factor passed straight into the accounting call | `run_override` | — (no basis claimed) |
| 2 | `BENCH_GRID_FACTORS` entry for the run's provider | `provider:<name>` | `BENCH_GRID_FACTORS[<name>]` |
| 3 | the self-hosted factor, on a local run (**legacy**) | `local_setting` | `BENCH_LOCAL_GRID_CO2E_G_PER_KWH` |
| 4 | the global default | `global_default` | `BENCH_GRID_CO2E_G_PER_KWH` |

Every run records **which rule applied**, not just the number it produced:
`grid_co2e_source` carries the stable key above and `grid_co2e_label` carries your
label when you set one, and both also appear on the `grid_intensity` provenance
record as `source_key` / `source_rule` / `source_label`. A provenance table can
therefore explain *why* a factor was used, which is the more interesting half once
several factors are configured and one run looks wrong.

`BENCH_LOCAL_GRID_CO2E_G_PER_KWH` and `BENCH_LOCAL_GRID_CO2E_BASIS` are
**legacy**: still read, still documented, and behaving exactly as they always
have for any provider without an entry of its own. `BENCH_GRID_FACTORS` with a
`"local"` key supersedes them and is strictly more expressive (it carries a
label), so prefer it in new configuration. Nothing is being removed.

### Why this is configuration and not geolocation

The obvious-looking feature here is to detect the caller's region and apply that
region's grid factor. Bench does not do this, and will not, and it is worth being
explicit because a reader will ask:

- **The caller's location is not the load's location.** For a cloud API call, the
  request is served by a data centre whose region has nothing to do with where the
  caller sits. Attributing a Toronto grid factor to inference served from Virginia
  is not an approximation; it is a different number about a different place.
- **Providers do not disclose the serving region** per request. There is nothing
  to read even if bench wanted to.
- **A router makes it worse.** OpenRouter sends a call to whichever upstream has
  capacity, so even the *provider* — let alone the region — can vary between two
  identical requests.
- **An IP lookup is also a network call and a privacy leak**, and bench's promise
  is that it makes no network calls except to the LLM providers you configure
  (plus an optional model-catalog fetch), with no telemetry ever. A geolocation
  dependency would break that for a number that would still be wrong.

Where location *is* knowable, the operator is the one who knows it: they
self-host somewhere specific, or they have pinned a provider to a region, or they
have a supplier disclosure in hand. So the factor comes from them. This adds
**zero network calls** — `BENCH_GRID_FACTORS` is parsed from the environment at
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

- A run recorded **before bench stored a basis** counts as its own group (`null`).
  It cannot be shown to share a basis with a location-based run, and assuming it
  does would be the same error in the other direction. A window of only such runs
  has one group, so it keeps its total.
- Per-row rollups follow the same rule and mostly keep their figures: a model
  belongs to one provider, so a `by_model` row usually stays summable even when the
  window does not. A harness that ran two providers does not, and says so.

### Regional sourcing

**Bench ships no external API integration for grid intensity, deliberately.** A
live dependency in the accounting path would make a stored run's carbon figure
depend on a third party's uptime, and each of these sources carries licence or
coverage limits an operator has to accept for themselves. The seam is
configuration: `BENCH_GRID_FACTORS` per provider, `BENCH_GRID_CO2E_G_PER_KWH`
globally (and the legacy local variant). You paste in a figure you sourced and can
defend; bench never fetches one.

| source | granularity | catch |
|---|---|---|
| [Electricity Maps](https://www.electricitymaps.com/) | hourly, per zone | free tier is one zone, non-commercial |
| [WattTime](https://watttime.org/) | marginal rate, sub-hourly | marginal ≠ average; a different question |
| [eGRID](https://www.epa.gov/egrid) / IEA | annual average | what most frameworks expect |

## Embodied hardware

`BENCH_EMBODIED_G_PER_RUN` defaults to **0**, which means local inference is
reported with **no manufacturing carbon at all**. That is a real understatement,
and it flatters exactly the option bench's own routing prefers.

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
offered because 0 is worse, not because it is good. Bench marks its confidence
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
misconfigured `BENCH_EMISSIONS_BASELINE_MODEL` pointed at a free model has no
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
cheap reasoning model saves dollars while costing more carbon, and bench
reports both rather than picking the flattering one.

### Zero-cost (local) models: a real 100%, and a deliberate asymmetry

A self-hosted model bills **$0** through bench's token API, so it can
legitimately read `avoided_usd_pct: 100.0` — "100% cheaper than frontier." That
figure is correct as far as it goes, and it does not go very far: it is **list-
price API spend only**. It excludes the electricity the machine actually drew
and any amortized hardware cost — bench does not model self-hosting's
electricity bill or capital cost, so those are not zero, they are simply not
counted in this figure. Every run with a zero-cost model carries a named caveat
saying exactly this, `money_excludes_self_hosting_costs`, with
`direction: "overstates"` — the real economic saving is smaller than 100% once
those costs are counted, even though bench cannot say by how much.

This is a **deliberate asymmetry** with the carbon accounting above, worth
stating plainly: the emissions model *does* attribute Scope 2 electricity (and,
if `BENCH_EMBODIED_G_PER_RUN` is set, embodied hardware) to a self-hosted run.
So the same run that reads "100% cheaper than frontier" in dollars can — and
typically does — carry a real, nonzero `co2e_g`. Money tracks what bench's
token API bills; carbon tracks what running the model actually draws. Neither
figure is wrong; they are answering different questions, and bench reports both
rather than letting the flattering one stand alone.

## Uncertainty: a band, not an interval

Every figure carries `co2e_g_low` / `co2e_g_high` at **central / 2.5** and
**central x 2.5**, configurable via `BENCH_UNCERTAINTY_BAND_LOW` / `_HIGH`.

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
and PSU losses invisible to software. Bench does not measure hardware at all.

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

Scopes are relative to a *reporting entity*. Here that entity is the **bench
operator**, not the model provider and not bench-the-project.

| scope | contents |
|---|---|
| **Scope 1** | always `0.0`, explicitly |
| **Scope 2** | electricity for self-hosted (`provider == "local"`) inference |
| **Scope 3** | all cloud inference; plus embodied hardware for local inference |

- **Scope 1 = 0** because running inference burns no fuel on the operator's
  premises. A nonzero Scope 1 could only come from on-site generation, which
  bench cannot observe and must not invent. It is reported as an explained zero
  rather than omitted: a missing scope reads as an oversight, an explained zero is
  a claim you can check.
- **Scope 2** is purchased energy. Self-hosting means the operator buys the kWh,
  so those emissions are theirs at the second scope. This is where
  `BENCH_GRID_FACTORS={"local":{…}}` belongs — or the legacy
  `BENCH_LOCAL_GRID_CO2E_G_PER_KWH`, which still works.
- **Scope 3** covers cloud inference as a *purchased service*: the provider's own
  Scope 1/2 becomes the operator's Scope 3 Category 1 (purchased goods and
  services). They never bought the electricity — they bought tokens. Local
  embodied hardware is Category 2 (capital goods).

So: **a cloud run is entirely Scope 3. A local run splits** — electricity into
Scope 2, embodied hardware into Scope 3. `co2e_g` is always exactly
`scope1_g + scope2_g + scope3_g`; that invariant is asserted across every
configuration in `backend/tests/test_emissions.py`.

## The counterfactual

For each run, bench re-prices **the identical token counts** through a baseline
model (by default the highest-energy-class curated non-local catalog entry, ties
broken by model id so the choice is deterministic across processes), in both
carbon and dollars.

- **Same-token, not same-task.** A different model would not produce identical
  token counts. A smaller model often needs more turns, or retries, or produces a
  worse answer someone redoes. Tokens are held fixed because that is the only
  comparison bench can make without guessing.
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
  names a model the catalog does not have, bench reports no comparison rather than
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
at training time, and a defensible lifetime query count. Bench has none of the
three, so it reports training as an explicit exclusion with a value of `0.0` and
confidence `excluded`, rather than picking a point in a 10,000x range.

### Cloud embodied hardware is not counted

It sits inside the purchased service (Category 1) and bench has no basis for
splitting it out, so a cloud run's Scope 3 is electricity-derived only. This
understates it.

### Reasoning tokens may not be in the counted output

Bench's token counts come from **provider usage reporting**. For several providers
hidden reasoning tokens are **not included in the billed output count**. Two
consequences, both named rather than absorbed:

1. Real generation work — and therefore energy — is **higher than counted** for
   those models. The bias is one-sided.
2. Energy per *visible* output token is **inflated** for these models, which is
   part of why the fitted `b` for o3 and DeepSeek-R1 is so much larger than for
   non-reasoning models. Some of that figure is work bench cannot see attributed
   to tokens it can.

This is recorded as the `reasoning_token_accounting` caveat on every run, and
strengthened on tier-R runs.

### Local inference is not batched

The class constants come from batched serving stacks (the reference data infers
batch sizes in the dozens; EcoLogits defaults to 64). A single-user local model
carries the whole accelerator for one request, so its real per-token energy can be
several times class S. Combined with embodied hardware defaulting to 0, **local
runs are the roughest estimate bench produces**, and both biases understate them.

### Everything else around inference

Excluded: water, network transfer, storage, retrieval and embedding calls, and the
router's own model call — the same scope as the dollar cost bench already reports.

## External anchors

Where bench's output sits against published per-prompt figures. These cover one
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
  market-based carbon, so its 0.03 gCO2e is not comparable with bench's
  location-based figure at all.
- Against Mistral, bench's figure for a comparable response is *lower*, and the
  reason is scope: Mistral's 1.14 gCO2e includes training amortization and
  embodied hardware, both of which bench excludes.
- **Anthropic publishes nothing.** This matters more than the other two rows,
  because bench's default models are Anthropic's — the models bench is most likely
  to be running are the ones with the least public data behind their energy class.

## Replacing a default with your own factor

Every default is replaceable, and each one is worth a different amount. In rough
order of payoff:

### 1. Grid intensity (biggest single win)

Per provider, which is the shape a real deployment has:

```
BENCH_GRID_FACTORS={"local":{"g_per_kwh":42,"basis":"location_based","label":"Ontario grid, IESO 2024"},"anthropic":{"g_per_kwh":120,"basis":"market_based","label":"provider PPA disclosure"}}
```

Or globally, which is still the fallback for every provider without an entry:

```
BENCH_GRID_CO2E_G_PER_KWH=<your region or supplier>
BENCH_GRID_CO2E_BASIS=location_based|market_based
```

The legacy self-hosted pair still works and is not going away:

```
BENCH_LOCAL_GRID_CO2E_G_PER_KWH=<your site factor>
BENCH_LOCAL_GRID_CO2E_BASIS=market_based
```

Get the numbers from eGRID (US subregional), your national inventory, your
supplier's disclosure, a provider's own published factor, or Electricity Maps /
WattTime if you accept their terms. **Say which basis each one is** — a
market-based figure mixed into a location-based total is not a smaller number, it
is a meaningless one, and bench will withhold the combined total rather than print
it (see [Basis separation](#basis-separation-what-may-be-added-to-what)).

A worked example. You self-host on an Ontario grid you have a published factor
for, and you also call Anthropic, whose PPA disclosure you accept:

```
BENCH_GRID_FACTORS={"local":{"g_per_kwh":42,"basis":"location_based","label":"Ontario grid, IESO 2024"},"anthropic":{"g_per_kwh":120,"basis":"market_based","label":"provider PPA disclosure"}}
```

A local run then records `grid_co2e_g_per_kwh: 42`, `grid_co2e_basis:
location_based`, `grid_co2e_source: provider:local`, `grid_co2e_label: "Ontario
grid, IESO 2024"`; an Anthropic run records 120 / `market_based` /
`provider:anthropic`. A window containing both has an energy total and a dollar
total but **no carbon total** — it has two, one per basis, in `by_basis`. That is
not bench being awkward; it is the GHG Protocol, and it is the reason the label
travels with every number.

### 2. Energy class per model

Meter your own deployment and put the result in `models.yaml` as
`energy_wh_per_mtok` (Wh per million output-equivalent tokens) for the models you
actually run. An explicit value always beats the class ladder. For open models,
Hugging Face's AI Energy Score is measured on controlled hardware and is better
evidence than bench's bucket.

If you cannot meter, at minimum review the class assignments: every one carries a
rationale comment in `models.yaml`, and the borderline calls (is this model
reasoning-tier or not?) move the figure by ~8x.

### 3. PUE

```
BENCH_DATACENTER_PUE=<your provider's disclosed figure>
BENCH_LOCAL_DEPLOYMENT_PROFILE=onprem_datacenter
BENCH_ONPREM_PUE=<your facility's measured PUE>
```
If you self-host anywhere other than a desk, switch the profile. If you have a
metered facility PUE, use it — it is one of the few inputs here you can actually
observe.

### 4. Embodied hardware

```
BENCH_EMBODIED_G_PER_RUN=<total embodied kg * 1000 / (lifetime runs * batch)>
```
Use your hardware's own published embodied footprint if the vendor discloses one.
Falling back to the H100/chassis constants above is defensible only with the
Boavizta caveat attached.

### 5. Uncertainty band

```
BENCH_UNCERTAINTY_BAND_LOW=2.5
BENCH_UNCERTAINTY_BAND_HIGH=2.5
```
Narrow it only if you have replaced the factors that justify its width — chiefly
metered energy and a regional grid factor. Widen it if you are running models
whose class you had to guess. Never relabel it as a confidence interval.

### 6. The baseline model

```
BENCH_EMISSIONS_BASELINE_MODEL=anthropic/claude-fable-5
```
Pick the model you would otherwise have used, if the auto-selected heaviest
catalog entry is not that.

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
  `provider:<name>` | `local_setting` | `global_default` | `run_override`) and
  `grid_co2e_label` (the operator's own note about it). A run recorded before
  these existed carries neither — read them as unknown, never as
  `global_default`;
- `cost` — money against the same-token baseline, including `avoided_pct` (the
  share of frontier spend avoided, one decimal place, null rather than `0%`
  when there is no baseline or the baseline itself costs nothing) — also
  reachable as `baseline.avoided_usd_pct`, the same figure;
- `uncertainty` — the band and its per-factor sensitivity;
- `factors` — **a list** (not an object: JSONB does not preserve key order) with
  one record per constant, each carrying `value`, `unit`, `source`, `url`, `date`,
  `confidence` and a `setting` to change it. Confidence is one of `exact`,
  `structural`, `calibrated`, `low`, `placeholder`, `excluded`. The
  `grid_intensity` record additionally carries `basis`, `overridden`,
  `source_key`, `source_rule` and `source_label`, and its `setting` names the one
  setting that actually applied rather than the three that might have;
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
  are same-token counterfactuals. Nothing bench reports removes carbon from the
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
profile to match where you actually run, and treat everything bench produces as a
starting sanity check rather than a result.
