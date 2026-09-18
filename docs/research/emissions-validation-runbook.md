# Emissions validation runbook

These commands operate on local, authorised exports. They do not connect to
production, call inference APIs, or certify accuracy. Run from `bench/backend`.
The preregistered protocol is [emissions-validation-protocol-v1.json](emissions-validation-protocol-v1.json).

## Workload inventory

Use `usage_export_record(run)` from `tret.services.emissions_validation` to
project database Run objects into a content-free export. The caller owns the
authorised query and time window. This projection excludes prompts, responses,
task inputs, credentials and documents. Missing revision, retry and tool data
remain unknown. A model alias is not an immutable model revision.

```sh
.venv/bin/python -m tret.services.emissions_validation inventory \
  --input /path/to/authorised-runs.jsonl \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z \
  --output /path/to/workload-inventory.json
```

The half-open window is validated against every exported row. Duplicate run IDs
are rejected. Run counts, token subtotals and energy reconcile without adding
segment energy to parent energy. Coverage and missing call metadata are counted;
run counts are not represented as API request counts.

## Measurement campaign

Match the experiment to the deployment being claimed. A local model server can
validate the local-serving estimator; it cannot establish the electricity used
by a proprietary hosted model. Hosted-model validation needs compatible
provider measurements or independently measured deployment evidence. Measuring
the Tret application server does not measure the remote model's inference.

For optional cumulative GPU counters, install `nvidia-ml-py` in the serving
environment and set `TRET_LOCAL_ENERGY_METER=nvml`. Select the GPU with
`TRET_LOCAL_ENERGY_METER_GPU_INDEX` (default 0). Unsupported counters use the
sampled-power fallback with diagnostics. Inspect timing/device coverage; a
reading that does not cover the claim is retained as diagnostic evidence and
does not replace its whole-run estimate. Validate this path on the actual rig.

For independent measurements, pass both Wh and the physical boundary:
`Router.run(..., measured_energy_wh=42.5, measured_energy_boundary="facility")`,
or `tret run ... --measured-wh 42.5 --measured-boundary facility`. Use `node_it`
for server-outlet/PDU readings excluding cooling. The meter location determines
the boundary, not its brand or instrument type.

1. Record exact model revision, engine/version, precision, GPU/count and workload.
2. Validate the instrument against a node or PDU reference. Save its validation
   ID, boundary, error and temporal/device coverage. GPU board counters alone do
   not establish node energy.
3. Submit disjoint `EnergyObservation` intervals and `WorkClaim` allocations to
   `tret.services.energy_allocation.allocate_energy`. Allocated, reserved and
   unallocated Wh must conserve the observed pool. Concurrent shareless requests
   remain unallocated; partial allocations do not replace a whole-run estimate.
4. Pilot warmup, steady state, idle, concurrency, switching and failed calls.
   Preregister repetitions after observing variance and meter precision.
5. Keep each prompt family and serving configuration in one split. Fit candidates
   on `fit`, tune on `validation`, then evaluate once on `held_out`.

An evaluation JSON/JSONL row requires:

```json
{
  "id": "measurement-1", "split": "held_out",
  "workload_family": "held-out-long-context", "serving_configuration": "held-out-rig-version",
  "boundary": "node_it", "observed_wh": 10, "predicted_wh": 11, "baseline_wh": 13,
  "instrument_validation_id": "reference-check-1", "reference_meter_id": "pdu-1",
  "temporal_coverage": 1, "device_coverage": 1,
  "cache": "warm", "reasoning": "high", "input": "long_context"
}
```

Values above illustrate the schema; they are **not experimental results**.

```sh
.venv/bin/python -m tret.services.emissions_validation evaluate \
  --input /path/to/measurements.jsonl --output /path/to/validation-report.json
```

The report includes signed bias, WAPE, median/p95 absolute error, interval
coverage, missing predictions and subgroup comparisons to the corrected ladder.
Zero observed energy makes percentage metrics undefined. Numerical targets
(±10% aggregate bias, ≤20% WAPE) cannot by themselves approve a release; review
instrument validity, repetitions and subgroup regressions. No richer estimator
is fitted or promoted automatically. Revisit at six months or a new hardware
generation.

## Cache experiment

Use `cache` with paired records containing `pair_id`, `model_revision`,
`configuration`, `prompt_hash`, `cache` (`on`/`off`), `boundary`,
`instrument_validation_id`, `observed_wh`, and both `temporal_coverage` and
`device_coverage` set to 1. Vary prompt length/hit rate and
randomize order. The tool reports a whole-request energy ratio. It does not turn
that ratio into a prefill coefficient: decode and overhead must first be
separated. The existing cache weight remains an explicitly unvalidated heuristic.

## Accepted-task cohorts

Use `cohort` with a JSON object containing `activities`, `deliverables`, and
`quality_gate`. Each activity needs unique `activity_id`/`accounting_id`, explicit
`deliverable_id`, status, boundary, factor basis, energy/carbon, and optional
`parent_id`. Include failed attempts, routing, compaction, child runs and material
tools as distinct self-only activities. Reject parent-inclusive plus child rows.
Each deliverable needs an explicit Boolean `accepted` and the same versioned
quality gate. Zero accepted deliverables yields an undefined intensity, while
failed work remains in totals. Attribution is supplied explicitly; conversation
membership alone is insufficient proof. Automatic production attribution and
quality-gate selection still need a representative export and product decisions.

## Shadow release

For older records without stored v1/v2 pairs, run the retrospective replay:

```sh
.venv/bin/python -m tret.services.emissions_replay \
  --input /path/to/content-free-accounting-export.jsonl \
  --output /path/to/retrospective-method-replay.json
```

This uses the recorded energy class, token buckets and factor weights. It never
loads current workspace settings or resolves an old model through today's
catalog. Multi-model segments must each retain their own accounting and their
token totals must reconcile with the parent. Measurements, overrides, estimated
segment usage and ambiguous records are excluded with counts. Unknown reasoning
semantics remain an explicit limitation. The report includes source hashes,
paired reconstructed estimates and cohort ranking changes. It is a retrospective
method comparison, not a prospective shadow window or measured validation.
The v1 coefficients retain facility-source calibration; v2 normalizes source
PUE to the IT boundary. The replay applies no deployment PUE, so its percentage
change is not a matched-boundary facility-energy comparison.

Keep private run exports and reports outside the public core repository.

Use `shadow` with run IDs and their full `energy_accounting` blocks. New v2
records retain `energy_method_shadow` with the paired v1 estimate. Reports count
missing pairs and show cohort ranking reversals as methodology effects.
`energy_strategy: class_ladder_v1` rolls back the estimator; `class_ladder_v2` is
the corrected default. Neither changes historical stored figures. Use the
existing what-if API for explicitly labelled restatement.

Deployment approval requires a representative shadow window and review of the
combined code/data diff. No production deployment or measured validation is
implied by local regression tests.
