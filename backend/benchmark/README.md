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
