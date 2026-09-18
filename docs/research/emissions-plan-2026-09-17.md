# Tret emissions accuracy: merged plan

**Date:** 2026-09-17. **Status:** software implementation/review and historical workload replay complete; measured accuracy validation remains open. Single
canonical plan. It merges, and supersedes, two documents written on
2026-09-15 and revised on 2026-09-17:

- [emissions-accuracy-plan-2026-09-15.md](emissions-accuracy-plan-2026-09-15.md)
  (work items `A0`–`A12`, revised 17 Sep with `A5a`, `A11a` and a review
  decisions section) and its companion
  [research and gap analysis](emissions-accuracy-research-2026-09-15.md).
- The "Tret Emissions Model Review" artifact (gaps `R-G1`–`R-G13` in the
  cross-reference below), whose plan section now points here.

The A-numbering is kept so the two earlier documents stay readable. Items
added by the merge are lettered (`A6a`, `A7a`, `A7b`). Where the two reviews
disagreed, the primary source decided:

- Jegham v1 Equation 1 combines GPU power, non-GPU power (at 0.5 utilisation)
  and per-company PUE ([§4.2](https://arxiv.org/html/2505.09598v1)). The class
  constants are therefore facility-level and Tret applies PUE twice. The
  artifact's original "host energy is missing, add a 1.7x serving overhead"
  claim is withdrawn.
- Google's August 2025 study includes embodied emissions in its lifecycle
  figures; do not add hardware on top of a Google total.
- Anthropic's usage contract exposes `output_tokens_details.thinking_tokens`
  and `usage.inference_geo`; the artifact's assumption that neither split was
  available is wrong for current API versions.

## Decision

Keep the factor-resolution, provenance and as-recorded history infrastructure.
Introduce an explicit contract for energy boundaries and evidence, correct the
source calibration, and validate a deployment-aware estimator against measured
workloads. Adopt newer coefficients only after matching units, boundaries,
denominators and workload conditions.

Two tracks share the first release:

- **Hosted-API track.** Most of Tret's retained traffic. Local application-server
  metering cannot validate remote inference; matching provider-side evidence is
  needed. This track ships corrected, cited, boundary-labelled fallbacks promptly and improves
  them through evidence (per-call upstream and geography, disclosures,
  licence-clean regional data), never through constants transplanted across
  boundaries.
- **Local-serving track.** Measurable, so it earns accuracy claims through a
  measurement contract, allocation reconciled to a reference meter, and a
  held-out calibration experiment.

Accepted from the reviews: correct the Jegham boundary; count reasoning tokens
once; capture upstream and geography per call; resolve the baseline through
the same layers as the run; give what-if explicit modes; activate regional
data for verified deployments; keep operational and lifecycle accounting
separable. Rejected: a generic `serving_overhead = 1.7`; a production embodied
estimate of 25% of operational; geography inferred from provider brand or
from an empty table; unknown values treated as zero; fitting per-prompt
medians and fixed-token observations as if they shared a denominator; any
forecast of how far the corrected numbers will move before the corrections
are measured. Deferred pending evidence: training allocation, hosted-provider
lifecycle totals, water.

Every release ships with a methodology-doc entry explaining what changed and
why, in the voice `emissions-methodology.md` already uses for corrections. A
number that falls because a method was wrong is a correction, never an
emissions reduction.

## Delivery sequence

| Phase | Work | Indicative effort | Exit condition |
|---|---|---|---|
| 0 | Workload inventory, pinned calibration evidence, decisions D1–D6 | 2–3 days | Dominant workload mix known; source observations pinned; decisions recorded |
| 1 | Correctness, boundary contract, usage and telemetry plumbing, citation fix | 1–2 weeks | No silent boundary conversion, no double PUE, no ambiguous measured scenario, no whole-segment upstream misattribution; baseline priced through the run's layers; grid default cited to its real source |
| 2 | Measurement and factor evidence | 1–2 weeks (local needs an NVIDIA box) | Reference workloads reconcile to meters; regional data covers verified deployments with licence and boundary recorded |
| 3 | Fit and evaluate estimator candidates | 1–2 weeks after phase 2 data | Held-out results support a new method or retain the corrected fallback; residuals and refit cadence published |
| 4 | Task comparison, uncertainty, conformance matrix, shadow rollout | 3–5 days | Comparable task metrics, validated coverage, version-aware release with rollback |

Dependencies: A1/A2 enable A3/A4; A5/A5a start early so observations exist
before calibration; A6/A7 feed A9/A10; A8/A11a lifecycle work is a separate
optional track. **A useful first release is A1–A4 plus A7a**, with A5/A5a
telemetry landing in parallel. Effort figures are planning estimates for
focused coder work, excluding review, hardware and benchmark compute.

## Implementation progress — 2026-09-17

The planned software foundation is implemented in the shared workspace. No deployment,
commit or historical restatement has occurred. Statuses distinguish executable
software from experiments that need actual data or hardware.

| Item | Current status | Evidence / remaining acceptance |
|---|---|---|
| A0 | Historical inventory executed | 478 retained hosted runs exported and reconciled; 442 stored energy estimates, zero measured observations. Sample concentration and missing deployment pins prevent a representative-production claim. |
| A1 | Implemented for selected calibration | Exact Jegham v1 inputs, reproducible v2 manifest, fit diagnostics and five-source candidate inventory. Candidate raw datasets are not imported or fitted. |
| A2 | Implemented | Energy boundaries, grid/gas/GWP metadata, factor provenance and missing component coverage. Instrument evidence still needs real validation. |
| A3 | Implemented | Default `class_ladder_v2` normalizes source PUE before the constrained fixed-weight fit; named v1 rollback and methodology shadow. No arbitrary serving/idle multiplier. |
| A4 | Implemented | Layer-consistent baseline and explicit measured preservation/re-estimation modes. Legacy ambiguous records remain excluded with reasons. |
| A5 / A5a | Implemented | Nullable reasoning semantics and per-call upstream/geography in providers, harness, SDK and local receipts; billing counts unchanged. Calls use their own time windows and upstream factors. Caveat direction ships as `either` for all reasoning cases, because the calibration denominator is unresolved — a recorded deviation from the written acceptance criterion (`overstates`, for counted-in-output providers). |
| A6 | Software implemented | Finite reading validation, sample-gap coverage, optional NVML runtime counter and sampled fallback, conserving interval ledger, central collector with ambiguous concurrency unallocated. Node/PDU reference campaign still required. |
| A6a | Experiment tooling implemented | Paired cache report distinguishes whole-request ratios from token weights. No measured replacement weight until the experiment runs. |
| A7 / A7a | Implemented | Pinned Ember World + 91 country observations for 2025; explicit country-ISO3 lookup; global 458.49; separate CC BY attribution and lifecycle-electricity metadata. Hourly factors integrate over modeled call intervals with an explicit constant-power assumption. Actual deployment-pin coverage needs A0 data. |
| A7b | Implemented | Disclosure-backed upstream PUE selection; no geography inferred. Unverified Microsoft operating-PUE figure excluded. The AWS entry is selected only when an upstream slug maps to `aws`; Bedrock's `amazon-bedrock` slug was unmapped, so it did not select in practice — fixed in this same batch by adding that mapping. |
| A8 | Implemented | Time/resource-share embodied allocator, legacy denominator metadata, supplied cloud coverage and double-add guards. No invented cloud hardware footprint. |
| A9 / A10 | Protocol and evaluation tooling implemented | Split leakage checks, held-out metrics, baseline/subgroup comparison, failure counts. Actual meter campaign, richer-model fitting, held-out results and supported-domain claims remain open. |
| A11 | Core tooling implemented | Explicit task activity ledger, failed-attempt numerator, duplicate guards, quality-gate denominator, corrected deliverable export and an evidence-class uncertainty seam (no production caller yet; the derived band equals the configured band until validated evidence is supplied). Production attribution still needs representative data and quality gates. |
| A11a | Coverage envelope implemented | Functional unit, included/excluded/unknown components, covered subtotal and withheld complete total. No automatic standards-conformity claim. |
| A12 | Retrospective replay executed; prospective shadow open | 414 eligible historical runs / 416 reconciled units; 64 excluded with reasons. Paired coefficients differ by −20.19%, from three causes — v1's hand-rounding removed, division by source PUE (roughly −11% to −12% per class on its own), and the switch from a two-parameter OLS to a single-coefficient fixed-0.05-input-weight fit — not from PUE normalization alone; class M (48.9% of v1 Wh) moves mostly on the fit-method change, class L moves less than PUE alone would predict because the fixed-weight fit raises it back up. No measured accuracy or savings claim. Representative prospective window and release review still required. |

The prior boundary/baseline batch passed 479 tests with one skip. The expanded
implementation has independent review and full-suite regression coverage; final
check results appear in Verification below. Tests establish software behavior,
not measured emissions accuracy.

### External validation still required

- Representative production activity and verified deployment pins beyond the completed retained-history export.
- A compatible local serving rig plus reference node/PDU meter, calibration
  evidence, and repeated benchmark observations.
- Accepted-task quality/latency gates and explicit attribution for production
  cohorts, including material non-LLM activity.
- A representative shadow window before deployment and any accuracy claim.
- Time-resolved energy observations for measured runs that cross grid intervals.
  Modeled calls use a labeled constant-power approximation; aggregate meter Wh
  and older records cannot establish their within-run power distribution.
- Candidate raw-data ingestion and richer estimator fitting only after compatible
  boundaries, denominators, licenses and validation data are available.

Tooling and schemas: [validation runbook](emissions-validation-runbook.md),
[preregistered protocol](emissions-validation-protocol-v1.json),
[candidate source inventory](../../backend/tret/data/calibration/candidate_anchors_2026-09-17.json).
These are prepared inputs and procedures, not measured results.

## Phase 0 — Establish what matters in Tret

### A0. Workload inventory

From an authorised local or production usage export, aggregate a
representative window by model revision, provider, upstream, deployment,
harness, token buckets, duration, retries and measured/estimated status.
Rank by requests and by estimated energy; neither alone is sufficient.
Deliver a coverage table: unknown reasoning tokens, unknown upstream, unknown
region, meter boundary, tool activity. Identifiers and aggregates only.

**Acceptance:** totals reconcile to the export; incomplete usage is counted;
denominator and window are explicit. Not a prerequisite for phase 1.

### A1. Pin calibration evidence

Create a reproducible manifest (`backend/tret/data/calibration/`) for the
Jegham v1 observation table, its Table 1 PUE values (OpenAI 1.12, Anthropic
1.14, DeepSeek 1.27), the 0.5 non-GPU utilisation assumption, units and
prompt shapes. Keep original and boundary-normalised observations as separate
columns. `JEGHAM_2025` in `emissions.py` loads from the manifest.

Add an anchors manifest for later use, each row carrying: GPU/node/facility
boundary; idle treatment; PUE; embodied treatment; input and output
denominators including thinking; workload definition and statistic (mean,
median, IQR); hardware, engine and batching; version and date; code and data
licences recorded separately. Candidates: Microsoft/Oviedo Joule study
([arXiv 2509.20241](https://arxiv.org/abs/2509.20241), modelled workloads),
Google August 2025 ([arXiv 2508.15734](https://arxiv.org/html/2508.15734v1),
comprehensive, market-based carbon, embodied included), Mistral LCA
(lifecycle), ML.ENERGY v3 rows (measured H100/B200, Apache 2.0), AI Energy
Score v2 rows (GPU energy, standardised tasks; star ratings are never
coefficients). Candidate status does not make a source a calibration input.
Never combine a per-prompt median with a fixed 1,000-in/1,000-out observation
as if they measured the same functional unit.

**Acceptance:** a reviewer regenerates every coefficient from immutable
inputs; every imported row has the metadata above; no unverified paper
identifier or magnitude enters the manifest.

## Phase 1 — Correctness before new coefficients

### A2. Define the energy and factor contract

**Owners:** `services/emissions.py`, `services/emission_factors.py`,
`services/energy_meter.py`.

Versioned metadata, design sketch to be fitted to existing conventions:

```text
method_id, method_version, calibration_id
energy_boundary: gpu | node_it | facility | partial | unknown
included_components, excluded_components
energy_source_by_component: measured | modeled | supplied | unknown
device_ids, measurement_interval, allocation_method, sensor_coverage
factor_boundary: generation | upstream | lifecycle | unknown
gas_coverage: co2 | co2e ; gwp_basis: ar5 | ar6 | unknown
includes_td_losses: bool ; electricity_mix_basis: production | consumption
accounting_basis, region, period, dataset_version
evidence_type: observed | configured | provider_asserted | fallback
```

Keep physical boundaries distinct from GHG Scope 1/2/3 categories. Define
`estimated` once: today `emissions.py:2467` sets it unconditionally even for
`energy_source: measured`; it should describe the carbon figure, with
component status in `energy_source_by_component`.

**Acceptance:** a GPU reading cannot occupy the complete-IT slot; a facility
figure cannot receive another PUE multiplication; a node reading is never
summed with its own GPU submeter; historical rows keep their numbers and get
`legacy/unknown` boundaries; unknown stays distinguishable from verified zero;
the settings API's `shipped_defaults` carries the new fields.

### A3. Refit the ladder with compatible boundaries

Normalise the pinned Jegham observations to node IT by dividing by source PUE
before fitting; retain the original as `class_ladder_v1` so stored runs and
what-if can name what priced them. Diagnostic reproduction from the 15 Sep
research doc, not a production table: GPT-4.1 nano 271.9→242.7, GPT-4o
1,233.1→1,101.0, Claude 3.7 Sonnet 2,634.7→2,311.1, o3 20,850.4→18,616.5,
DeepSeek-R1 35,474.8→27,932.9. Evaluate a constrained non-negative fit and a
fixed-overhead term; do not assume a two-parameter fit to three points is
robust. XL stays interpolated and says so. Do not reclassify the result as
accelerator-only and do not apply a Google-derived multiplier: Google's
1/0.58 is facility-inclusive and cannot be multiplied in and then receive PUE.

Idle and reserve capacity are outside this calibration. Decision D5 retains
a multiplier of 1.0: no generic 1.1 adjustment or managed-layer reserve claim
is shipped. Deployment evidence must distinguish idle/reserved capacity before
a future explicit scenario or measured allocation is introduced.

The `active_params` strategy stays labelled a GPU-only component until
GPU-count and host terms exist; it never silently occupies the complete-IT
slot.

**Acceptance:** fixtures show source→IT conversion and exactly one deployment
PUE application; `energy_wh_total / energy_wh == pue` and nothing else
multiplies overhead in; coefficient changes are published as methodology
effects without rewriting history.

### A4. Correct measured scenarios and baseline factor resolution

**Owners:** `api/analytics.py`, accounting metadata, `emissions.py:2146`
(`_baseline_block`).

What-if gains explicit modes: `preserve_measured_energy` (keep observed Wh,
change only compatible carbon-side factors; refuse a PUE change on a
facility-boundary observation) and `estimate_both_sides` (today's behaviour,
named). Raw measured values are immutable.

`_baseline_block` prices the counterfactual with `resolve_grid_factor(...,
settings)`, `pue_for(..., settings)`, `embodied_g_for(..., settings)` and the
catalog model's own `energy_wh()`, bypassing workspace, managed and harness
layers, regions, hourly tables, `model_overrides` and `energy_strategy`. Build
a second `FactorSet` for the baseline model and provider through the identical
layers at the run's `at`; store both sides' provenance; preserve intentional
differences such as another provider's region.

**Acceptance:** an identity scenario returns the recorded figure for a
measured run in preserve mode; factor-only changes cannot invent a meter
reading; mixed segments keep their own source types; excluded runs are
identical on both sides. A test with a workspace grid override, a run on
model A and a baseline model B on the same provider prices both at the
override; the same-model zero case is no longer the only baseline test.

### A5. Resolve provider usage semantics

**Owners:** `providers/`, `engine/harness.py`.

Retain raw usage and normalise disjoint buckets: fresh prefill, cached prefix,
cache write, visible generation, and a nullable reasoning count with
provider-specific semantics (subset of billed output, or additional).
Anthropic exposes `usage.output_tokens_details.thinking_tokens`, including on
the final `message_delta`
([extended-thinking usage](https://platform.claude.com/docs/en/build-with-claude/extended-thinking));
OpenAI-compatible providers may expose
`completion_tokens_details.reasoning_tokens`. Pin API versions in fixtures.
Absence is unknown, not zero, and a missing split does not mean those tokens
were omitted from the total. Capture reasoning effort as metadata. Audit
retry and failure paths for omitted partial work.

Then correct the caveat direction per provider. Today every R-class run
carries `reasoning_token_accounting` with `direction: understates`. Where the
provider bills thinking inside output and the class was fitted per visible
token, the direction is `overstates`. Emit `reasoning_counted_in_output` or
`reasoning_hidden`, never both. Test the double-counting hypothesis by
establishing which count Jegham's output shapes represent. The R coefficient receives the same source-PUE boundary correction as the
other anchors. A reasoning-denominator adjustment waits for A10 and compatible evidence; neither an 8x
error nor a collapse toward another class is established today.

**Acceptance:** fixtures cover (1) output including reasoning, (2) reasoning
reported separately and additionally, (3) reasoning unreported, (4) input
including cached tokens, (5) separate cache counts, (6) older responses
without detail fields. Streaming final and cumulative events are idempotent;
thinking plus visible output reconciles per pinned semantics; every known
token is counted once; costs and existing totals are unchanged; no reasoning
multiplier is applied on top of separately counted reasoning work.

### A5a. Capture per-call upstream and inference geography

**Owners:** canonical usage, provider adapters, `engine/harness.py`,
segment and accounting persistence.

Add per-call nullable `served_by` and `inference_geo` without changing routing
or request settings. `ModelSegment.served_by` records only the last completed
upstream turn and factor resolution uses the catalog provider; a segment that
spans upstreams must be split or persisted per call. Never apply the final
`served_by` to the whole segment. Anthropic documents a global default,
explicit `us`, and response `usage.inference_geo`
([data residency](https://platform.claude.com/docs/en/manage-claude/data-residency)).
`global` is not a zone; `us` is not a regional-grid pin; provider identity is
not region; residency at rest is not inference location. Preserve
`evidence_type` for observed, configured, provider-asserted and fallback
values. Resolve the upstream inside the existing layer precedence so explicit
overrides still win and unknown upstream takes the documented fallback.

**Acceptance:** fixtures cover an upstream change within one run, unknown
metadata, layer precedence and old rows; each call or split segment carries
only its own evidence; instrumentation changes no routing behaviour.

### A7a. Grid default citation — decision D1

The shipped 470 gCO2e/kWh is labelled "IEA 2024 global average". IEA
*Electricity 2025* gives 445 gCO2/kWh for 2024 and *Electricity 2026* gives
435 for 2025, both generation-only CO2, estimated. The pinned Ember release gives 471.46 (revised 2024) and
458.49 (2025) gCO2e/kWh under CC BY 4.0. Its methodology includes lifecycle
electricity-generation emissions, not generation-only CO2. No number is mandated by this plan; the
label, boundary and gas coverage must match whichever source is chosen, with
`factor_boundary`, `gas_coverage`, `includes_td_losses` and the observation
year recorded. Ship with the A2 contract so the fix cannot recur.

## Phase 2 — Establish trustworthy observations

### A6. Validate local metering and allocation

Prefer validated NVML cumulative energy counters; keep sampled power with
coverage diagnostics. Add CPU/RAM/other-node measurement or labelled
estimates through an adapter interface (CodeCarbon is a candidate, with its
fallback provenance exposed) and compare to a node or PDU reference. One
collector per shared device allocates its energy once across concurrent
requests; allocation plus unallocated/reserved reconciles to the measured
pool. Measure warmup, steady state, idle, concurrency and model switches
separately.

**Acceptance:** allocated plus unallocated matches the source window within
an empirically recorded tolerance; resets, negative deltas, non-finite values,
unsupported devices and gaps are handled; a missing reading is never zero.

### A6a. Cache-read energy experiment

No published energy figure exists for a KV-cache hit relative to fresh
prefill; Tret's 0.005 weight mirrors provider pricing. On the A6 rig, run a
local vLLM with prefix caching on and off, same prompts, and derive the ratio
across context lengths and hit rates. A whole-request on/off ratio is not a prefill weight. Separate decode and
overhead before fitting a replacement weight; otherwise keep the existing
heuristic explicitly unvalidated. A validated replacement is deployment-specific. Tret's harness
runs re-read large cached contexts every turn, so this weight matters more
here than for a chatbot.

### A7. Activate regional factor data for verified deployments

**Owners:** `services/grid_zones.py`, `grid_tables.py`, `grid_regions.py`,
`tret_cloud/emissions/managed_factors.json`.

First map the regions actually needed: configured pins, A5a `inference_geo`
values seen in A0, and verified local sites. Then assess sources for those
regions, recording code and data licences separately; an Apache-licensed
importer grants no rights to its data, and ODbL is not automatically a
blocker but its share-alike obligations for a derived table must be recorded
([ODbL 1.0 §2.3, §4.4](https://opendatacommons.org/licenses/odbl/1-0/)).
Candidates with unambiguous redistribution terms: Ember yearly (CC BY 4.0,
country level, lifecycle electricity-generation GHG intensity), Our World in Data (CC BY, lifecycle), EPA
eGRID2023 rev2 (public domain, 27 US subregions, grid-gross-loss kept
separate), UK DESNZ 2026 (OGL), Google `region-carbon-info` (Apache 2.0, per
GCP region), Cloud Carbon Footprint coefficients (Apache 2.0, AWS/Azure
regions). Electricity Maps (ODbL) remains a candidate where sub-national or
hourly coverage is needed and its obligations are accepted. Use the existing
offline import path; do not begin with a multi-source importer rebuild.

Keep generation-only and lifecycle factors distinguishable with provenance
and year. Resolve upstream and region only from configuration evidence or
A5a metadata. An empty table does not prove production defaults, and provider
brand does not justify defaults such as Anthropic-US or Mistral-France;
provider-published serving statements may be recorded as
`evidence_type: provider_asserted` entries only under decision D3. Unknown
hosted regions keep a labelled fallback or scenario range.

**Acceptance:** a supported pin resolves to a dated, licence-recorded,
boundary-labelled figure; an unsupported pin reports its fallback;
typical-day profiles and dated observations have distinct metadata; long runs
integrate across intervals; no live network dependency in run execution.

### A7b. Managed-layer disclosures — decision D3

The Kith managed document ships `grid.providers: {}` under the rule "no entry
without a dated disclosure". Disclosure-backed entries that satisfy the rule
without inferring geography: per-upstream fleet PUE with URL and `as_of`
(Google 1.09 for 2024 and AWS 1.14 for 2025), applied by observed A5a upstream,
never guessed from the model brand. Google's observed `google-vertex` slug maps
explicitly to that disclosure. Microsoft 1.17 and the proposed EU aggregate
were not verified for compatible use and are not shipped. Provider
serving statements (Anthropic global default with explicit `us` option;
Mistral EU/US regional endpoints; Bedrock global cross-region inference and
Vertex global endpoints recorded as `region: nondeterministic`) are
`provider_asserted` evidence and ship only if D3 is accepted. Disclosures live in tret-cloud; core resolves them through the existing
precedence layers. No provider region is inferred.

### A8. Improve embodied coverage

Define whether `runs_over_lifetime` counts requests or batches before keeping
the current divisor (kg / (runs × batch) under-allocates if runs already
means requests). Prefer equipment footprints allocated by time and reserved
resource share, with lifetime sensitivity. Add cloud hardware as a separately
supplied or estimated component when evidence permits; do not force it to
zero because the customer does not own the equipment. Manufacturing
allocation is independent of the runtime grid factor. A heuristic such as
`embodied = 0.25 × operational` may appear only as an explicit low-confidence
scenario, never as a production estimate. Source candidates: Cloud Carbon
Footprint's Teads-derived coefficients (Apache 2.0); Boavizta only after its
data licence is confirmed (its API is AGPL-3.0).

**Acceptance:** a full equipment lifetime recovers its footprint once;
batching cannot divide the same workload twice; changing the grid factor
leaves embodied unchanged; supplier totals that already contain hardware
(including Google lifecycle totals) receive no addition; unsupported
components stay excluded/unknown with visible coverage; amortised service
footprints stay separate from corporate capital-goods reporting.

## Phase 3 — Build a calibration experiment

### A9. Benchmark design

Select workloads from Tret's task distribution. Starting matrix:

| Dimension | Initial coverage |
|---|---|
| Input length | Short, typical, long-context, real upper tail |
| Output length | Short, normal, long generation |
| Reasoning | Off/low/high where supported; preserve emitted usage semantics |
| Cache | Cold prefill, warm prefix, mixed hit rates |
| Serving | Single request plus representative concurrency/batch levels |
| Deployment | Dominant local configurations; hosted only where useful evidence exists |
| Hardware/software | Exact model revision, GPU/count, precision, engine version |
| Operational | Warmup, steady state, idle, retries and failed tasks |

Screen a subset first; increase repetitions until variance and instrument
behaviour are understood; capture windows long enough to measure short calls
while keeping request traces for allocation. Compare three candidates:
corrected ladder; non-negative prefill/decode/context model (a per-turn
context term is adopted only if held-out residuals fall); architecture-aware
model with completed GPU-count and host boundaries. Evaluate energy first,
then apply one compatible grid factor to isolate energy-model error from
carbon-factor differences.

### A10. Pre-register validation criteria, refit, and cadence

Separate fit, validation and held-out sets; hold out prompt shapes and
serving configurations, not random near-duplicates. Report signed bias,
median absolute error, weighted absolute percentage error, tail error,
interval coverage and failures by subgroup; avoid MAPE near zero. Initial
targets, subject to meter precision: aggregate bias within ±10% and weighted
absolute percentage error ≤20% for supported measured classes. No material
regression against the corrected ladder on important groups. Publish long
context, cache hit and reasoning errors separately.

Refit the hosted ladder only on the `node_it` boundary with compatible
denominators (A1 manifest, A5 counts). Add a golden-run fixture set so any
refit is a reviewable diff, and write a refit cadence into the doc: every six
months or on a new hardware generation, whichever first. Hosted APIs keep
fallback status regardless; if the richer model fails held-out validation,
ship the corrected ladder and improved coverage instead.

## Phase 4 — Useful comparisons and controlled release

### A11. Task-level accounting and uncertainty

Reconciled activity tree (main inference, routing, compaction, child runs,
retries, material non-LLM work), reusing `Run.conversation_id` attribution
where it helps. Define accepted-task quality and latency gates. Report
absolute Wh/g, Wh/g per accepted task, success rate and missing coverage; for
a cohort, divide all attributable energy including failed attempts by
accepted deliverables; if none succeed, the rate is undefined and the total
and failure count are reported. Name the same-token counterfactual as such; a
routing-savings claim needs observed baseline tasks.

Replace evidence-by-label narrowing with evidence classes tied to A6/A10
validation. Separate measurement error, model error, deployment uncertainty
and missing-component coverage; correlated factor scenarios in rollups;
distributions only where evidence supports them. Preserve location, market
and consequential views and withhold incompatible totals.

**Acceptance:** failed work cannot vanish from both numerator and coverage
record; duplicate child/run accounting is detected; cohorts share quality
gate and boundary; a method change is shown separately from an operational
improvement.

### A11a. Boundary and conformance matrix

Define separate deliverables: operational inference estimate, full lifecycle
inference result, provider model-development result, SCI for AI consumer
score. The [SCI for AI specification](https://github.com/Green-Software-Foundation/sci-ai/blob/e8d3534f72b26e7b114c9054050db60f4543bb60/SPEC.md)
separates provider development from consumer operation and includes embodied
hardware in the consumer calculation; a consumer score is not an
electricity-only estimate and training is not automatically amortised into
every inference. The implementation pins repository revision `e8d3534f72b26e7b114c9054050db60f4543bb60`
for its incomplete coverage mapping; ratification and conformity are not claimed.
Publish a scope/coverage matrix, not a blanket claim: functional unit (one
run; one deliverable via A11), lifecycle stages, included and excluded
components, factor boundary and GWP, evidence coverage, and the two standards
being watched: CEN-CENELEC M/593 (AI energy measurement, targeted Q4 2026)
and the GHG Protocol consolidated corporate standard (consultation Q2 2027,
publication Q4 2028 per its 29 July 2026 FAQ). Water is reported separately
in physical units if at all. Add training allocation only when the chosen
boundary requires it and the data is defensible.

**Acceptance:** every result names its functional unit, stages, components
and coverage; conformance claims cite a pinned revision and only the covered
boundary; a first release may improve operational accuracy without waiting
for water or training accounting.

### A12. Shadow release and review

Run the proposed method beside legacy for a representative window; store both
with method versions; compare ranking reversals and error by workload; keep
the as-recorded default view; offer labelled restated estimates; provide
rollback. Before merge: reviewer pass over the combined code and data diff,
update `emissions-methodology.md` (including sections that still say nothing
is metered), model comments and user-facing claims.

**Acceptance:** traceable factors for every result; supported-domain accuracy
demonstrated; missing components visible; comparisons use compatible
boundaries; no release claim rests on unit tests alone.

## Decisions recorded for this implementation

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Grid default source and label (A7a) | Resolved | Ember 2025, exactly 458.49, lifecycle electricity-generation CO2e; pinned source hash. |
| D2 | Electricity Maps as bundled source (A7) | Resolved for this release | Existing source kept separate; new Ember country observations have their own CC BY provenance. No new ODbL-derived rows imported. |
| D3 | Provider disclosures (A7b) | Resolved | Ship cited Google/AWS operating-fleet PUE as provider-asserted evidence, selected only by observed upstream. Region remains explicit configuration/evidence. |
| D4 | Reasoning-token and source-PUE corrections (A3, A5) | Resolved | Correct source boundaries and count confirmed additional reasoning once. No unsupported reasoning-denominator adjustment or accuracy claim. |
| D5 | Idle/reserve adjustment (A3) | Resolved | No additional multiplier (1.0). No generic 1.1 managed factor; wait for deployment evidence. |
| D6 | Lifecycle scope (A8, A11a) | Resolved for this implementation | Operational covered subtotal with optional supplied embodied allocation; complete lifecycle total withheld while components are missing. |

## Cross-reference

| Item | 15 Sep plan / research | Artifact review |
|---|---|---|
| A0 | A0 | — |
| A1 | A1 | R-G4 anchors |
| A2 | A2, G2 | R-G5 fields, R-G12 |
| A3 | A3, G1 | R-G3 (corrected), idle/reserve |
| A4 | A4, G10, G11 | R-G3b, what-if modes |
| A5 | A5, G3 | R-G2 |
| A5a | A5a | R-G6 |
| A6 | A6, G2 | measured-path boundary |
| A6a | G3 cache note | R-G9 |
| A7 | A7, G4 | R-G7, R-G1 |
| A7a | G5 | R-G5 |
| A7b | — | R-G8 |
| A8 | A8, G7 | R-G11 |
| A9, A10 | A9, A10 | R-G4, R-G10, golden runs |
| A11 | A11, G9, G8 | per-deliverable unit |
| A11a | A11a | R-G12 conformance |
| A12 | A12 | — |

## Verification — final local batch, 2026-09-17

- Bench backend full suite: **3,081 passed, 44 skipped**, using
  `cd bench/backend && .venv/bin/pytest -q`.
- Managed-factor checks: **9 passed**, using
  `cd tret-cloud && .venv/bin/pytest -q tests/test_managed_factors.py`.
- Independent review closed all material findings; its final focused pass
  covered 215 tests plus the frontend build.
- Ruff passed for every changed/new Python file; `git diff --check` passed in
  both repositories.
- Frontend TypeScript and production build passed with `npm run build`.
- `uv build --wheel --out-dir /tmp/tret-emissions-wheel-check` passed. The wheel
  contains all 11 required new calibration/grid assets and validation modules.
- Existing dependency deprecation warnings and the frontend large-chunk warning
  remain; no production execution or empirical accuracy claim follows from these checks.
- An independent audit ([emissions-audit-2026-09-17.md](emissions-audit-2026-09-17.md))
  replicated every quantitative claim in this plan and the private validation
  results, and listed a ranked set of fixes before commit. This fix batch
  addresses those findings.

Regression coverage includes facility readings without double PUE, missing usage
with retained measurements, additional reasoning without extra billing, per-call
upstream/timing factors, hourly transitions and table gaps, incompatible carbon
rollups with retained Wh and money, measured what-if metadata, strict embodied
inputs, meter resets/startup/teardown timing, allocation conservation, task
attribution and validation-data split isolation.

The earlier 479-test result remains historical evidence for the first batch;
the full-suite result above supersedes it for this implementation. No commit,
deployment or historical restatement has occurred.

## Next empirical work package

1. **Historical inventory completed:** 478 retained cloud runs; collect a broader, labelled production sample with verified deployment pins to establish representativeness.
2. Validate GPU/node/PDU instruments and allocation on the serving rig; collect
   the preregistered cache/context/reasoning/concurrency observations.
3. Fit candidates on the training split; review held-out errors and retain v2
   if a richer estimator fails the criteria.
4. Define task quality/latency gates and complete the attribution ledger using
   representative production activities.
5. Observe a representative v1/v2 shadow window, review ranking changes and
   remaining coverage, then make the deployment decision.

No synthetic measurements, inferred geography, or unit-test pass is substituted
for these acceptance criteria. CPU/RAM/node coverage may be supplied through the
external boundary-aware reading contract; no unvalidated automatic host-energy
adapter or full lifecycle estimate is claimed.


## Historical validation execution — 2026-09-17

Completed a content-free SELECT-only export from the running hosted database,
then the offline inventory and strict coefficient replay. Private artifacts and
results live outside the public repository at
`validation-artifacts/emissions-2026-09-17/validation-results.md` under the shared
Tret workspace. No database updates, inference calls, deployment or hardware
provisioning occurred. Earlier “no production execution” verification statements
refer to the software-test batch; this later step read actual retained records.

- **478 runs**, dated 25 August–11 September; 442 stored estimates and **zero
  measured energy observations**. All lack explicit method IDs, energy
  boundaries and model revisions; none contain per-call records.
- **414 replayable runs / 416 units**, using recorded class-ladder factor
  provenance and token weights; 64 excluded. Billed-output reasoning inclusion
  is unknown for all paired units. Segment tokens reconcile to their parents.
- Coefficient outputs total **634.272 Wh (v1)** versus **506.192 Wh (v2)**,
  **−20.19%**. This has three components, not one: v1's hand-rounding is
  dropped, the fitted mean is divided by its source PUE (roughly −11% to
  −12% per class on its own), and the fit itself switches from a
  two-parameter OLS to a single nonnegative coefficient against the fixed
  predictor `output + 0.05 × input`. Per class, in Wh/Mtok:

  | Class | v1 shipped | v1 fitted mean | ÷ source PUE | v2 shipped |
  |---|---:|---:|---:|---:|
  | S | 250 | 271.9 | 242.7 | 210.3 |
  | M | 1,200 | 1,233.1 | 1,101.0 | 855.7 |
  | L | 2,600 | 2,634.7 | 2,311.1 | 2,399.3 |
  | R | 21,000 | 20,850.4 | 18,616.5 | 17,713.3 |

  Class M — 48.9% of v1 Wh across the replay's 42 units — dominates the
  aggregate movement, and most of its −28.69% is the fit-method change and
  de-rounding, not the PUE boundary correction; class L moves *less* than
  PUE division alone would predict, because the fixed-weight fit pushes its
  constant back up. No target-deployment PUE or grid factors are applied by
  this replay. This is method sensitivity, not a matched-boundary facility
  comparison, measured accuracy gain, carbon change or emissions saving.
- No model-cohort total ranking reversals; workloads and volumes differ, so
  this is not a model-efficiency ranking.
- Cache reads are **71.44%** of input-side tokens: prioritize paired cache
  measurements. **91.84%** of runs occurred on one day and **71.34%** have the
  same final model. Test/user traffic is undistinguished; representativeness
  is not established.
- Export transport used a SELECT-only plain transaction and timestamp cutoff,
  with no enforced database snapshot across pages. All 20 pages completed and
  478 IDs were unique; concurrent update consistency is not guaranteed.
- Added strict offline replay and regression coverage; **28 focused tests
  passed**. Independent review covered export projection and replay eligibility.

**Still needed:** matching provider energy evidence or a compatible serving
rig with reference measurements; actual empirical fitting and held-out error;
representative prospective shadow traffic; accepted-task attribution. Local
application-server or unrelated-model measurements cannot validate these hosted
models. No matching rig, reference observations or provider energy export was
available during this execution.
