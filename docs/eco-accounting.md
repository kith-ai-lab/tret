# Routing objectives and ecological accounting

Bench reports two costs for every run: dollars, and estimated energy/carbon.

## Why bench reports energy at all

Bench's flagship domain is climate risk. A platform whose deliverables are TCFD
sections and divergence verdicts cannot treat its own compute footprint as
somebody else's problem — a climate document that hides what it burned is making
an argument it would not accept from a portfolio company. So the footprint is
reported in the run audit view, in the deliverable's provenance appendix, and in
the deliverable body itself.

The second reason is that the estimate is *actionable*. Model choice moves the
number by more than an order of magnitude, and bench already picks the model.
Turning "use a smaller model when a smaller model will do" into a knob is worth
more than perfect measurement of a choice nobody can change.

Everything below is an estimate. Bench says "estimated" everywhere it prints
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
for. It defaults to `balanced`, which is exactly bench's historical behavior —
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
keep bench's curated-first preference: a hand-checked entry outranks a
dynamically discovered one. The thrift objectives **drop** curated-first and keep
curation only as a tiebreak — if an uncurated OpenRouter entry really is cheaper
or lower-energy, an objective that asked for cheap or low-energy has to be
allowed to pick it, otherwise the catalog quietly overrules the operator.
Separately, price stands in for capability throughout: bench holds no benchmark
score for a model, and what a lab charges is the most honest proxy on hand.

An unrecognized objective is a 422 from the harness API, never a silent fall back
to `balanced`. Quietly substituting a different objective is precisely the class
of behavior this platform exists to rule out.

## The energy model

Each catalog model carries an `energy_class` and an `energy_wh_per_mtok`
(watt-hours per million tokens processed). Classes are declared per curated model
in `providers/models.yaml`; the class-to-Wh table lives in
`services/emissions.py` (re-exported from `providers/catalog.py`, where callers
have always found it):

| class | Wh/Mtok | typical members |
|---|---|---|
| S | 50 | small, distilled, or quantized weights — including local models |
| M | 300 | mid-size served models |
| L | 1200 | large frontier models |
| XL | 3000 | largest frontier / heavy-reasoning models |

**Where the numbers come from.** M is the anchor: ~300 Wh/Mtok is ~0.3 Wh for a
~1k-token prompt, the order of magnitude large operators have published for a
median text prompt on a mid-size served model. S/L/XL step from there by model
scale, roughly a factor of 4–6 per step, matching how per-token inference energy
scales with active parameter count. Individual class assignments come from public
model-scale signals: parameter counts where they are known, price and latency as
proxies where they are not.

**Unclassified entries** get a class from their cost tier — dynamic OpenRouter
models by price (economy→M, standard→L, premium→XL), local models S (small
quantized weights on end-user hardware). Every default is a positive number: a
zero *dollar* price never means zero energy, and local inference is free in
dollars but not in watts.

A model may override its class default with an explicit `energy_wh_per_mtok` in
`models.yaml` — do that if you have a real measurement for your deployment.

### Per-run math

```
weighted_tokens = input + output + 0.1 × cache_read + 1.0 × cache_write
energy_wh       = energy_wh_per_mtok × weighted_tokens / 1e6   # compute / IT load
energy_wh_total = energy_wh × PUE                              # + facility overhead
co2e_g          = energy_wh_total × grid_co2e_g_per_kwh / 1000 + embodied_g
```

Cache reads are discounted to 0.1×, mirroring how bench prices them: a read
re-uses stored KV state instead of running a fresh forward pass over those
tokens. A cache *write* is a full forward pass and carries full weight. Both the
per-run figure and its full derivation are persisted (`runs.energy_wh`,
`runs.energy_accounting`) and exposed by the runs API.

`energy_wh` is **compute (IT-load) energy only** and always has been.
`energy_wh_total` adds the data-centre overhead (`datacenter_pue` 1.2 for cloud,
`local_pue` 1.05 for self-hosted), and it is the figure carbon comes off.
`co2e_g` is the run total and always equals `scope1_g + scope2_g + scope3_g`.
The scope mapping, the PUE reasoning and the `embodied_g` term are all in
[emissions-methodology.md](emissions-methodology.md).

### Grid intensity

`BENCH_GRID_CO2E_G_PER_KWH` (setting `grid_co2e_g_per_kwh`, default `400.0`)
converts energy to carbon. 400 gCO2e/kWh is roughly the world-average grid
intensity; a regional or provider-specific figure is much better (~30 for
Sweden, ~380 for the US average, ~700 for a coal-heavy grid).
`BENCH_LOCAL_GRID_CO2E_G_PER_KWH` optionally overrides it for self-hosted runs,
where the operator buys the power and may hold a site- or market-based factor;
unset, local runs use the same figure as cloud runs. The intensity in force at
run time is stored inside `runs.energy_accounting`, so changing the setting later
does not silently rewrite history — and the `/api/analytics/emissions` rollup
sums those stored figures rather than recomputing at today's settings.

## Limits — read this before quoting a number

- **These are estimates, not measurements.** No provider publishes per-model
  energy draw. Nothing in bench is metered.
- **Inference energy depends on things bench cannot see**: hardware generation,
  batch size, sequence length, quantization, accelerator utilisation, whether
  your request landed on a warm replica. Data-centre overhead is *modelled* by a
  default PUE rather than ignored, but that PUE is a self-reported fleet average,
  not the building that served your request.
- **Class assignment is judgement.** Model sizes are often undisclosed; price is
  a proxy for scale and an imperfect one.
- **Reasoning tokens can dominate.** A model that thinks at length before
  answering can burn several times the energy of one that does not, and bench
  only sees the tokens a provider reports.
- **Training is excluded.** This is inference only, amortized training energy is
  not allocated.
- **The router's own call is not counted.** A run's figure covers the execution
  model's turns, not the small routing call that chose it — the same scope as the
  dollar cost bench already reports.
- **Local models are the roughest estimate here.** S assumes small quantized
  weights on typical end-user hardware; a 70B model on a workstation GPU is well
  outside that.
- **Local models exclude embodied hardware by default** (`embodied_g_per_run` is
  0), which understates them — the honest caveat is spelled out in
  [emissions-methodology.md](emissions-methodology.md).
- **Not an emissions figure of record.** These numbers are good enough to compare
  two candidate models, or to see one harness burning ten times another. They are
  not good enough for a disclosure, and bench does not present them as such.
  Nothing bench reports — including `avoided_co2e_g` — is an offset, a credit, or
  an emissions reduction.

If you need defensible numbers, measure your own deployment and put the result in
`energy_wh_per_mtok`, then read
[emissions-methodology.md](emissions-methodology.md) for the other defaults you
must replace.
