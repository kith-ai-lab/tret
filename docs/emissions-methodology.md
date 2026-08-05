# Emissions methodology

How bench turns token counts into a carbon figure, every constant it uses, and —
more important — what the resulting number is *not* good for.

Implementation: `backend/bench/services/emissions.py`. Settings:
`backend/bench/config.py`. The energy model itself (classes, token weighting) is
described in [eco-accounting.md](eco-accounting.md); this document covers the
carbon layer on top of it: data-centre overhead, the GHG Protocol scope split,
and the frontier-baseline counterfactual.

**One-line summary: these are order-of-magnitude estimates for comparing model
choices. They are not measurements, not an inventory, and not reportable.**

## Read this first

Bench's flagship domain is climate risk, which is exactly why this page leads
with limits rather than headline numbers. A platform that produces TCFD sections
would not accept an unfalsifiable carbon claim from a portfolio company, so it
must not make one about itself. Everything here is derived from token counts and
published heuristics; nothing is metered.

## The chain, end to end

```
weighted_tokens  = input + output + 0.1 x cache_read + 1.0 x cache_write
energy_wh        = energy_wh_per_mtok x weighted_tokens / 1e6      # compute / IT load
energy_wh_total  = energy_wh x PUE                                 # + facility overhead
electricity_g    = energy_wh_total x grid_co2e_g_per_kwh / 1000
embodied_g       = embodied_g_per_run                              # local runs only
co2e_g           = electricity_g + embodied_g                      # == scope1 + scope2 + scope3
```

`energy_wh` keeps the meaning it has always had — **compute (IT-load) energy
only**. `energy_wh_total` is the PUE-inclusive figure, and it is the one carbon is
derived from. Both are persisted, so the overhead is never hidden inside a single
number.

## Every constant

| constant | setting | default | why this value | honest uncertainty |
|---|---|---|---|---|
| Energy class S | `models.yaml` | 50 Wh/Mtok | small / distilled / quantized weights, incl. local models | factor of ~3–5 either way |
| Energy class M | `models.yaml` | 300 Wh/Mtok | the anchor: ~0.3 Wh for a ~1k-token prompt, the order of magnitude large operators have published for a median text prompt on a mid-size served model | factor of ~3 either way |
| Energy class L | `models.yaml` | 1200 Wh/Mtok | one scale step up (~4x) from M, matching how per-token inference energy tracks active parameter count | factor of ~3–5 either way |
| Energy class XL | `models.yaml` | 3000 Wh/Mtok | largest frontier / heavy-reasoning models | factor of ~5, and reasoning-heavy runs can blow through it |
| Cache-read weight | — | 0.1x | a read re-uses stored KV state instead of a fresh forward pass; mirrors bench's 0.1x price discount | the true ratio is implementation-specific; it is not 0 |
| Cache-write weight | — | 1.0x | a write *is* a full forward pass | low uncertainty; this one is close to structural |
| Cloud PUE | `datacenter_pue` | 1.2 | hyperscalers self-report fleet-wide PUE of roughly 1.1–1.2; 1.2 is a mildly conservative pick inside the published band | real facilities range from ~1.1 (new purpose-built campus) to ~1.6+ (older leased space). Self-reported, fleet-average, and not verifiable per request |
| Local PUE | `local_pue` | 1.05 | a desktop or workstation has almost no facility overhead — fans and a share of room cooling | 1.0–1.3 depending on whether the room is actively cooled |
| Grid intensity | `grid_co2e_g_per_kwh` | 400 gCO2e/kWh | rough world-average grid intensity | ~30 (Sweden) to ~700+ (coal-heavy). A regional or supplier factor is *much* better; this default can be off by 10x+ |
| Local grid intensity | `local_grid_co2e_g_per_kwh` | unset → falls back to the above | lets a self-hosting operator use their own site- or market-based factor (supplier mix, PPA, on-site solar) | as good as the operator's own figure |
| Embodied carbon | `embodied_g_per_run` | 0.0 g | not counted by default: bench has no basis for guessing your hardware or its lifetime | **a real understatement.** See the caveat below |
| Baseline model | `emissions_baseline_model` | "" → highest-energy-class curated non-local model, ties broken by model id | a deterministic, auditable stand-in for "if we had just used the biggest thing available" | it is a counterfactual, not a measurement of anything |

A PUE below 1 is physically impossible (a facility cannot use less than its IT
load), so a misconfigured value below 1 is clamped to 1 rather than allowed to
shrink the number. A negative `embodied_g_per_run` is clamped to 0: nothing in
this module is allowed to subtract carbon.

## GHG Protocol scope mapping

Scopes are always relative to a *reporting entity*. Here that entity is the
**bench operator** — the organisation running bench — not the model provider and
not bench-the-project. With that fixed, the mapping follows from the Protocol's
own definitions (direct emissions; purchased energy; other value-chain
emissions):

| scope | what bench puts in it | reasoning |
|---|---|---|
| **Scope 1 = 0, always** | nothing, reported explicitly as `0.0` | Scope 1 is direct emissions from sources the entity owns or controls. Running inference burns no fuel on the operator's premises. A nonzero Scope 1 could only come from on-site generation (a diesel generator, a gas CHP unit), which bench cannot observe and must not invent. It is reported as an explicit zero with an explanation string rather than omitted, because a missing scope reads as an oversight while an explained zero is a claim you can check. |
| **Scope 2** | electricity for **self-hosted** (`provider == "local"`) inference | Scope 2 is indirect emissions from purchased energy. When the operator runs the model on their own hardware, they buy the kWh, so those emissions are theirs at the second scope. This is where `local_grid_co2e_g_per_kwh` belongs: an operator with a supplier-specific or market-based factor should use it here. |
| **Scope 3** | (a) **all** cloud inference (anthropic / kimi / openrouter), (b) amortized embodied hardware for local inference | (a) Cloud inference is a *purchased service*. The provider's own Scope 1 and Scope 2 emissions from serving the request are the operator's Scope 3 under Category 1, purchased goods and services. The operator never bought the electricity — they bought tokens. (b) Manufacturing the operator's own inference hardware is Category 2, capital goods, amortized per run. |

So: **a cloud run is entirely Scope 3. A local run splits — electricity into
Scope 2, embodied hardware into Scope 3.** `co2e_g` is always exactly
`scope1_g + scope2_g + scope3_g`; that invariant is asserted in
`backend/tests/test_emissions.py`.

Two deliberate exclusions, stated rather than buried:

- **Cloud embodied hardware is not separately estimated.** It is conceptually
  inside the purchased service (Category 1) and bench has no basis for splitting
  it out. So a cloud run's Scope 3 is electricity-derived only, which understates
  it.
- **Training is not allocated.** This is inference accounting. Amortized training
  emissions belong to the provider's footprint and would be part of the
  operator's Category 1 in a fuller inventory; bench does not attempt it.

## Embodied hardware: the honest caveat

`embodied_g_per_run` defaults to **0**, which means local inference is reported
with **no manufacturing carbon at all**. That is a real understatement, and it
matters because it flatters exactly the option bench's own routing prefers: a
small local model looks close to free, when its hardware carried a substantial
one-off manufacturing footprint before it ran a single token.

If local-vs-cloud comparison is the point, set the figure. A rough approach:
take the device's published embodied carbon (workstations and GPUs are typically
in the low hundreds of kgCO2e) and divide by the runs you expect over its
lifetime — e.g. 300 kgCO2e over 100,000 runs is 3 gCO2e per run, which is the
same order as the electricity for a small model. Bench reports whatever you set
under Scope 3 and shows it as `embodied_g` on every run.

## The frontier-baseline counterfactual

For each run, bench re-prices **the identical token counts** through a baseline
model (by default the highest-energy-class curated non-local catalog entry, ties
broken by model id so the choice is deterministic across processes), then reports:

```
avoided_co2e_g = baseline_co2e_g - actual_co2e_g
```

Definition and limits, which travel with the number in its `basis` string:

- **Same-token, not same-task.** A different model would not produce identical
  token counts for the same task. A smaller model often needs more turns, or
  retries, or produces a worse answer someone has to redo. The counterfactual
  holds tokens fixed because that is the only comparison bench can make without
  guessing.
- **It is an efficiency indicator.** It says "this run was lighter than the
  heaviest option, by roughly this much, at equal token counts". That is useful
  for model selection, and it is all it is.
- **It is not an offset, not a credit, not an emissions reduction, and not a
  saving.** No carbon was removed from anywhere. Nothing here may be netted
  against a footprint, and it must not appear in statutory or regulatory
  reporting.
- **It is signed.** A run on something heavier than the baseline reports a
  *negative* avoided figure. Bench does not clamp it to zero — clamping would
  turn an honest arithmetic result into a one-way marketing number.
- **A run on the baseline model itself reports exactly 0**, because comparing a
  run to itself contains no counterfactual.
- **An unresolvable baseline reports `null`, not 0.** If
  `emissions_baseline_model` names a model the catalog does not have, bench
  reports no comparison rather than silently substituting another model.

## Window rollups are as-recorded

`GET /api/analytics/emissions` sums each run's **stored** figures, frozen at the
factors in force when that run happened. It does not recompute anything at
current settings.

This is a deliberate correction of an earlier flaw. Recomputing a window at
today's grid factor means that correcting a setting silently rewrites history,
and makes the aggregate disagree with the per-run figures the runs API and
deliverable provenance already show. So:

- `totals` are plain sums of stored per-run values.
- `factors` reports **current settings, for reference only** — nothing in the
  response was computed from them — plus `factors.recorded`, the actual
  `(deployment, grid intensity, PUE)` combinations the window contains.
- `factors.mixed_factors` is `true` when the window contains more than one such
  combination, and the disclaimer says so. A window mixing a corrected grid
  factor, or mixing cloud and self-hosted runs (which use different factors by
  design), has no single honest factor behind its total.
- Runs with **no** estimate are excluded from every total and counted in
  `totals.runs_without_estimate`. `null` is not `0`: a run without an estimate is
  not a run that emitted nothing.
- Runs recorded before the scope split or the baseline existed keep their
  `co2e_g` and are counted in `runs_without_scope_split` /
  `runs_without_baseline`. Their scopes are **not** back-filled, so in such a
  window `scope1+scope2+scope3` is deliberately less than `co2e_g`, and the
  disclaimer says why.
- The scan is **bounded to the most recent 2000 runs in the window**
  (`EMISSIONS_RUN_SCAN_LIMIT`), because as-recorded rollups read each run's JSON
  block in Python rather than using JSON predicates only Postgres would honour.
  The bound, the rows actually scanned, and whether the result was truncated are
  reported in `scan`.

The older `GET /api/analytics/guardrails` energy block is unchanged and is
*not* this: it sums the `energy_wh` column (compute only, no PUE, no embodied,
no scopes) and derives carbon at today's factor. Its response says so and points
here. Expect its carbon figure to be lower than this one.

## Limitations — read before quoting any number

- **Not audit-grade.** Nothing here is metered, verified, or assured. It is a
  model of a model.
- **Not an offset and not a reduction claim.** `avoided_co2e_g` is a
  same-token counterfactual (see above). Nothing bench reports removes carbon
  from the atmosphere or may be netted against anything.
- **Not for statutory or regulatory reporting** — not CSRD, not SEC climate
  rules, not GHG Protocol inventory submission — unless you first replace every
  default with metered energy for your own deployment and supplier-specific or
  regional grid factors, and can defend the energy classes for the models you
  actually used.
- **Token-based estimation ignores a great deal**: batching and concurrency,
  hardware generation, accelerator utilisation, idle draw between requests,
  speculative decoding, model parallelism, cooling water, network transfer,
  storage, and the energy of everything around inference (retrieval, embeddings,
  the router's own call — which is excluded, matching the scope of bench's dollar
  cost).
- **Reasoning tokens can dominate** and bench only sees what a provider reports.
- **Energy-class assignment is judgement.** Model sizes are often undisclosed;
  price stands in for scale and is an imperfect proxy.
- **Local models are the roughest case.** Class S assumes small quantized weights
  on typical end-user hardware; a 70B model on a workstation GPU is well outside
  that, and embodied hardware is excluded by default.
- **Cloud runs assume a single global grid factor**, because bench does not know
  which region served a request. Providers' own regional and market-based
  disclosures are strictly better information.
- **PUE is self-reported and fleet-averaged**, not the PUE of the building that
  served your request.

If you need defensible numbers: meter your own deployment, put the result in
`energy_wh_per_mtok` for the models you run, set a supplier-specific grid factor
(and `local_grid_co2e_g_per_kwh` for self-hosted), set `embodied_g_per_run` from
your own hardware, and treat everything bench produces as a starting sanity
check rather than a result.
