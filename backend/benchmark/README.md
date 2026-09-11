# The harness benchmark

**Question under test:** same model, same data — what changes when the only
difference is the harness?

This directory holds the outcome benchmark for tret: cases, both runners,
scoring, and (eventually) results. It ships in the repo on purpose. If a claim
in our marketing rests on a number produced here, anyone must be able to rerun
it. Status: **scaffold — no results yet. No outcome claims may cite this
directory until a full run is written up, limitations included.**

## Design

Two arms per case, per model:

| | Arm A — harnessed | Arm B — unharnessed |
|---|---|---|
| Model | identical | identical |
| Data access | `lookup_dataset` tool calls | the same CSVs pasted into context, in full |
| Rules | doctrine loaded by the pack | the same doctrine text pasted into context |
| Output | `record_verdict`, JSON-schema enforced, cited values mechanically cross-checked | asked for the same JSON shape, nothing enforced |
| Missing data | `file_data_request` tool available | told it may respond `insufficient_data` and describe what's missing |

Arm B is deliberately a **strong** baseline: full data, full doctrine, a
carefully written prompt, and explicit permission to decline. A strawman
baseline would be its own kind of dishonesty, and someone will check.

## Cases

28 signal-divergence assessments over the climate-risk pack's sample data
(`packs/climate-risk/sample-data/`), enumerated in `cases.yaml`:

- **22 covered cases** — every site × peril pair that has a vendor score,
  including the interesting ones (stale 2018 vintage against a strongly
  trending signal; scores whose method notes flag scale mismatches).
- **5 missing-reference probes** — site exists, peril is assessable, no vendor
  score exists (S-004 has no vendor coverage at all).
- **1 missing-site probe** — a site id that appears in no dataset.

The 6 probes are the honesty test: the correct answer is *ask, don't guess*.

## Metrics

1. **Fabricated-number rate** — numeric values in the output that appear
   nowhere in the source data. Scored mechanically; borderline tokens
   (legitimate arithmetic like a computed difference) go to a manual
   adjudication list rather than being silently counted either way.
2. **Misattribution rate** — a real value cited against the wrong column
   (a vintage year presented as a hazard score).
3. **Guess-vs-ask on the probes** — did the arm emit a verdict, or say
   `insufficient_data` / file a data request?
4. **Verdict agreement** — against blind expert labels (`labels.yaml`,
   completed before any model output is looked at; template in
   `labels.template.yaml`).
5. **Cost and energy per run** — recorded by tret for Arm A; token counts ×
   list price for Arm B.

## Blind labeling protocol

The expert (domain founder) fills in `labels.template.yaml` → `labels.yaml`
**before any benchmark run is executed or examined**, using only the pack's
sample data and doctrine — the same inputs both arms get. `labels.yaml` is then
committed, so the git history proves the labels predate the runs.

## Running

```
# Arm A needs the tret stack up (docker compose up) and admin credentials.
TRET_URL=http://localhost:8000 python arm_a.py --model <model-id> --out results/arm_a.<model>.jsonl

# Arm B needs an OpenRouter key.
TRET_OPENROUTER_API_KEY=... python arm_b.py --model <model-id> --out results/arm_b.<model>.jsonl

python scoring.py results/*.jsonl --labels labels.yaml --report results/report.md
```

## Arm Routing — the router against pinned models

A third arm, orthogonal to A/B above: it holds the harness fixed and asks
whether *routing itself* is earning what it costs. For every case it drives
the real engine through the API (same as Arm A) under a matrix of temporary
harness configurations built on the `Climate Analyst` base harness — `auto`
routing under each objective the router supports (`quality`, `balanced`,
`token_conservation`, `eco`) and `pinned` routing to specific models (default:
the catalog's cheapest economy-tier model and its most expensive premium-tier
model). Every configuration is its own harness (same pack/tools/task profile,
only `model_policy` differs), created or updated through
`POST`/`PUT /api/harnesses` so a rerun reuses rather than duplicates it.

```
# Needs the tret stack up (docker compose up) and admin credentials, same as Arm A.
TRET_URL=http://localhost:8000 python arm_routing.py --cases cases.yaml \
    --out results/routing.jsonl

# Pin specific models instead of the catalog-derived defaults, and/or send
# each case as a multi-turn conversation to exercise the prompt cache:
python arm_routing.py --pins openrouter/openai/gpt-5-mini,anthropic/claude-opus-5 \
    --turns 3 --out results/routing.jsonl

python scoring_routing.py results/routing*.jsonl --labels labels.yaml \
    --summary results/routing_summary.json
```

**Row schema** (one JSON line per case × configuration × turn — see
`arm_routing.py`'s own docstring for the full field list): `case_id`,
`config` (`"auto:<objective>"` or `"pinned:<model_id>"`), `config_kind`,
`objective`, `pin`, `turn`, `turns_total`, `run_id`, `status`, `chosen_model`,
`effort`, `context_fit_mode`, `fallback_used`, `switches_count`,
`switches_reasons`, `effort_changes_count`, `provider_ignore`, `served_by`
(one entry per `model_timeline` segment), `cost_usd`, `reported_cost_usd`,
`router_overhead_usd`, token totals (`input_tokens`/`output_tokens`/
`cache_read_tokens`/`cache_write_tokens`), `cache_ledger` (null when the run
carries no `model_timeline` to sum it from), `compactions_count`,
`iterations`, `verdict_payload`, `validation_error_count`, `wall_ms`, `error`.
`arm`/`model` are also set (to `"routing"`/`config`) purely so
`scoring.score_case` can score a routing row exactly like an Arm A/B one.

**What the columns mean**: `chosen_model`/`effort`/`context_fit_mode`/
`fallback_used`/`provider_ignore` are read straight off the run's persisted
`routing` decision; `switches_count`/`effort_changes_count` count mid-run
interventions (the supervisor changing model or raising reasoning effort);
`router_overhead_usd` is the router's *own* LLM call cost (`run.overhead`),
kept separate from `cost_usd` because it runs on a different model. The
scoring side (`scoring_routing.py`) turns these into a per-configuration table
(delivered rate, verdict agreement, mean cost, cache hit ratio, switch rate,
effort mix) and a router-vs-pinned table: for each `auto:<objective>`
configuration, cost (with router overhead folded in) and agreement relative
to whichever pinned configuration in the same result set had the best
agreement and whichever was cheapest.

**Caveat: this arm costs real money, multiplicatively.** Each case runs once
per configuration (objectives + pins) per turn — a 28-case set, the default 4
objectives plus 2 pins, and `--turns 1` is already 168 real model calls (times
`--turns` for a multi-turn run). Start with `--only` on a handful of cases
before a full sweep.

## Stated limitations (these travel with any published result)

- Same-model comparison only. This measures what the harness adds, not whether
  either arm beats a human analyst. No human arm exists.
- N=28, one task family (signal divergence), one domain pack, sample data.
- Arm A's grounding metrics are enforced by the same machinery being measured;
  the meaningful Arm A number is how many attempted violations the harness
  *caught*, which is read from run validation events, not assumed.

## Pilot checklist (week 2, before any full run)

- Verify `arm_a.py`'s findings and validation-event read paths against a live
  transcript (marker strings are noted inline).
- Tune `scoring._plausibly_derived`: with this many source numbers, sums and
  differences cover most small integers, so today it routes nearly every
  fabricated integer to manual adjudication instead of the fabricated count.
  Conservative (nothing miscounted automatically), but the pilot should decide
  whether to restrict derivation to within-dataset pairs.
- Confirm `labels.yaml` is committed before the first full run is executed.
