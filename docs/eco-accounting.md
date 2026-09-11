# Routing objectives and ecological accounting

Tret reports two costs for every run: dollars, and estimated energy/carbon.

## Why tret reports energy at all

Tret's flagship domain is climate risk. A platform whose deliverables are TCFD
sections and divergence verdicts cannot treat its own compute footprint as
somebody else's problem — a climate document that hides what it burned is making
an argument it would not accept from a portfolio company. So the footprint is
reported in the run audit view, in the deliverable's provenance appendix, and in
the deliverable body itself.

The second reason is that the estimate is *actionable*. Model choice moves the
number by more than an order of magnitude, and tret already picks the model.
Turning "use a smaller model when a smaller model will do" into a knob is worth
more than perfect measurement of a choice nobody can change.

Everything below is an estimate. Tret says "estimated" everywhere it prints
these numbers, and you should too.

**This page covers routing objectives and the energy model.** The carbon layer on
top of it — data-centre overhead (PUE), the GHG Protocol scope split, the
frontier-baseline counterfactual, the `/api/analytics/emissions` rollup, and the
uncertainty band on every constant — is in
[emissions-methodology.md](emissions-methodology.md). Read that one before
quoting a carbon figure anywhere. The two pages share one set of numbers; where
they overlap, the methodology page is authoritative.

## Routing objectives

`model_policy["objective"]` on a harness tells the router what it is optimizing
for. It defaults to `balanced`, which is exactly tret's historical behavior —
existing harnesses route unchanged.

| objective | what it optimizes | candidate ordering |
|---|---|---|
| `quality` | the most capable candidate within the cost tier; thrift is secondary | curated first, then most expensive (capability proxy), newer first |
| `balanced` (default) | the cheapest model that will do the job well, newer preferred | curated first, then cheapest output price |
| `token_conservation` | small-but-sufficient models with disciplined output | cheapest output price, then lowest energy, curation as tiebreak |
| `eco` | least estimated energy per unit of work | lowest Wh/Mtok, then cheapest, curation as tiebreak |

The objective threads through four places, because a preference that only
appeared in one of them would be advisory rather than real:

1. **Candidate ordering** (`router_llm/objectives.py`, used by
   `ModelRouter._candidates`). The list shown to the router model is ordered and
   then truncated, so ordering decides what gets considered at all.
2. **The rendered prompt** (`router_llm/prompts.py`). Non-default objectives add
   an `OBJECTIVE` section of preference rules; `eco` and `token_conservation`
   also add each candidate's energy class to the candidate list. `balanced`
   renders the prompt byte-for-byte as before, since its rules already *are* the
   router system prompt's ordering.
3. **The deterministic fallback** (`router_llm/fallback.py`). The shape table is
   a capability ranking, so the thrift objectives rank the usable catalog
   directly instead of walking it; `quality` climbs the table rather than taking
   the first working entry.
4. **The persisted decision**. `RoutingDecision.objective` lands in
   `runs.routing`, so an audit can tell which objective was in force — the same
   candidates under a different objective are a different decision.

Two notes on the interaction with the curated catalog. `balanced` and `quality`
keep tret's curated-first preference: a hand-checked entry outranks a
dynamically discovered one. The thrift objectives **drop** curated-first and keep
curation only as a tiebreak — if an uncurated OpenRouter entry really is cheaper
or lower-energy, an objective that asked for cheap or low-energy has to be
allowed to pick it, otherwise the catalog quietly overrules the operator.
Separately, price stands in for capability throughout: tret holds no benchmark
score for a model, and what a lab charges is the most honest proxy on hand.

An unrecognized objective is a 422 from the harness API, never a silent fall back
to `balanced`. Quietly substituting a different objective is precisely the class
of behavior this platform exists to rule out.

## Reasoning effort, and the verdict shape's own tier table

`RoutingDecision.effort` (`router_llm/objectives.py::default_effort`) is the
other lever the objective pulls, alongside candidate ordering: `low`/`medium`/
`high` reasoning-effort, forwarded to whichever provider control the chosen
model supports (Anthropic's `output_config.effort`, OpenRouter's unified
`reasoning.effort`). `quality` always asks for `high`; `token_conservation`
and `eco` always ask for `low` — **except on the `verdict` shape**, added
2026-09-11 after gemini-3.8-flash (a `standard`-tier model) spent 31k–74k
output tokens and $0.14–0.34 per case at a flat `high` default without
landing more designed cases than a cheaper effort would have.

Verdict-shape effort is instead a function of the **chosen model's own cost
tier** (not the harness's `max_cost_tier` ceiling):

| chosen model's tier | effort | quality objective |
|---|---|---|
| `premium` | `high` | `high` |
| `standard` | `medium` | `high` (kept) |
| `economy` / `local` | `low` | `low` |

A cheap model does not get more disciplined by being asked to think harder,
only more expensive — so every objective but `quality` follows the table
exactly, and `quality` itself still drops to `low` once the model is
economy/local tier, even though it keeps `high` on `standard`. A caller with
no model chosen yet (the router prompt's own EFFORT section, rendered before
a candidate is picked) reads the harness's `max_cost_tier` as a stand-in and
gets `high` for an unset or unrecognized tier — the historical always-high
assumption.

`token_conservation` still forces `low` on every *other* shape — a drafting
or extraction task under this objective never climbs above it, verdict-shape
tier table aside.

## Telling the model itself to conserve tokens

Candidate ordering and effort both act *before* the model sees a prompt —
which model runs the turn, and how hard it is allowed to think. Neither says
anything about output length once the model is actually writing, and on two
`token_conservation` drafting runs the cheap model chosen wrote *more* than a
`balanced` run on the same prompt. `engine/context.py::assemble_context` now
appends a third, in-prompt lever when the run's objective is
`token_conservation`: an `objective_guidance` context block (accounted in
`context_composition` like every other block) telling the model to answer in
the fewest words that fully answer the task, skip preamble and restated
questions, prefer a short list to prose, and keep every quoted value exact —
brevity trims words, never the numbers, ids, or filenames a citation reports.

## The energy model

Each catalog model carries an `energy_class` and an `energy_wh_per_mtok`
(watt-hours per million tokens processed). Classes are declared per curated model
in `providers/models.yaml`; the class-to-Wh table lives in
`services/emissions.py` (re-exported from `providers/catalog.py`, where callers
have always found it):

| class | Wh/Mtok | typical members |
|---|---|---|
| S | 250 | small, distilled, or quantized weights — including local models |
| M | 1,200 | mid-size served models |
| L | 2,600 | large frontier models |
| XL | 6,000 | largest non-reasoning frontier models |
| R | 21,000 | the reasoning tier |

The unit is Wh per million **output-equivalent** tokens (see the per-run maths
below): generation costs roughly 20x reading, so the buckets are not summed 1:1.

**Where the numbers come from.** Each class except XL is a least-squares fit of
`Wh = a x input + b x output` against Jegham et al., arXiv:2505.09598 — S from
GPT-4.1 nano, M from GPT-4o, L from Claude 3.7 Sonnet, R from o3. XL has no
measured anchor and is interpolated one step above L. Two of the five fits came
out degenerate, and the input weight is therefore a documented assumption rather
than a measurement. The full working, the residuals and the caveats are in
[emissions-methodology.md](emissions-methodology.md) — read it before quoting a
class figure.

**The reasoning tier is assigned from what a model does, never from its price.**
In the reference dataset DeepSeek-R1 is among the two heaviest models measured
*and* among the cheapest sold, so tret's cheapest curated model carries its
heaviest class. Individual assignments come from public model-scale signals and
product positioning; each one carries its rationale as a comment in
`models.yaml`.

**Unclassified entries** get a class from their cost tier — dynamic OpenRouter
models by price (economy→M, standard→L, premium→XL), local models S (small
quantized weights on end-user hardware). Every default is a positive number: a
zero *dollar* price never means zero energy, and local inference is free in
dollars but not in watts.

A model may override its class default with an explicit `energy_wh_per_mtok` in
`models.yaml` — do that if you have a real measurement for your deployment.

### Per-run math

```
weighted_tokens = 0.05 × input + 1.0 × output
                + 0.005 × cache_read + 0.05 × cache_write
energy_wh       = energy_wh_per_mtok × weighted_tokens / 1e6   # compute / IT load
energy_wh_total = energy_wh × PUE                              # + facility overhead
co2e_g          = energy_wh_total × grid_co2e_g_per_kwh / 1000 + embodied_g
```

An output token is the unit and weighs 1.0; an input token weighs 0.05 because
prefill is parallel while generation is autoregressive (the fitted ratio is ~20x).
A cache *write* is a full prefill pass, so it weighs exactly what input does. A
cache read is discounted a further 10x, mirroring how tret prices it: the read
re-uses stored KV state instead of running a fresh forward pass. It is not free.
Both the per-run figure and its full derivation are persisted (`runs.energy_wh`,
`runs.energy_accounting`) and exposed by the runs API.

`energy_wh` is **compute (IT-load) energy only** and always has been.
`energy_wh_total` adds the data-centre overhead (`datacenter_pue` 1.2 for cloud;
for self-hosted, `local_pue` 1.05 on a workstation or `onprem_pue` 1.56 in a
machine room), and it is the figure carbon comes off. `co2e_g` is the run total
and always equals `scope1_g + scope2_g + scope3_g`. The scope mapping, the PUE
sourcing, the `embodied_g` term, the uncertainty band and the money-saved figure
are all in [emissions-methodology.md](emissions-methodology.md).

### Grid intensity

`TRET_GRID_CO2E_G_PER_KWH` (setting `grid_co2e_g_per_kwh`, default `470.0`)
converts energy to carbon. 470 gCO2e/kWh is the IEA's 2024 global power-sector
average; a regional or supplier-specific figure is much better (~30 for Sweden,
~350 for the US average, ~750 for a coal-heavy grid, and EPA eGRID subregions
span more than 10x). `TRET_LOCAL_GRID_CO2E_G_PER_KWH` optionally overrides it for
self-hosted runs, where the operator buys the power and may hold a site- or
market-based factor; unset, local runs use the same figure as cloud runs.

Each factor also carries a **GHG Protocol basis** label
(`TRET_GRID_CO2E_BASIS`: `location_based` | `market_based` | `unspecified`),
because a market-based figure and a location-based one answer different questions
and must never be summed. The intensity and its basis in force at run time are
stored inside `runs.energy_accounting`, so changing the setting later does not
silently rewrite history — and the `/api/analytics/emissions` rollup sums those
stored figures rather than recomputing at today's settings.

## Limits — read this before quoting a number

- **These are estimates, not measurements.** The class constants are fitted to
  measured latency on *inferred* hardware for five models and generalised well
  beyond them. Nothing in tret is metered, and every figure carries an explicit
  ±2.5x judgment band that is not a confidence interval.
- **Inference energy depends on things tret cannot see**: hardware generation,
  batch size, sequence length, quantization, accelerator utilisation, whether
  your request landed on a warm replica. Data-centre overhead is *modelled* by a
  default PUE rather than ignored, but that PUE is a self-reported fleet average,
  not the building that served your request.
- **Class assignment is judgement.** Active parameter counts are unpublished for
  every closed model in the catalog, so tier and price stand in for scale and do
  so imperfectly.
- **Reasoning tokens can dominate, and may not be counted.** A model that thinks
  at length can burn 8x the energy of one that does not — hence the separate R
  class. Worse, several providers exclude hidden thinking tokens from the billed
  output count tret reads, so for those models the real work is *higher* than
  counted. That bias is one-sided and is named on every run rather than absorbed.
- **Training is excluded.** This is inference only, amortized training energy is
  not allocated.
- **The router's own call is not counted.** A run's figure covers the execution
  model's turns, not the small routing call that chose it — the same scope as the
  dollar cost tret already reports.
- **Local models are the roughest estimate here.** S assumes small quantized
  weights on typical end-user hardware; a 70B model on a workstation GPU is well
  outside that. The class constants also come from *batched* serving stacks, and
  single-user local inference carries the whole accelerator for one request — so
  local figures understate by several times before embodied hardware is even
  considered.
- **Local models exclude embodied hardware by default** (`embodied_g_per_run` is
  0), which understates them — the honest caveat is spelled out in
  [emissions-methodology.md](emissions-methodology.md).
- **Not an emissions figure of record.** These numbers are good enough to compare
  two candidate models, or to see one harness burning ten times another. They are
  not good enough for a disclosure, and tret does not present them as such.
  Nothing tret reports — including `avoided_co2e_g` — is an offset, a credit, or
  an emissions reduction.

If you need defensible numbers, measure your own deployment and put the result in
`energy_wh_per_mtok`, then read
[emissions-methodology.md](emissions-methodology.md) for the other defaults you
must replace.
