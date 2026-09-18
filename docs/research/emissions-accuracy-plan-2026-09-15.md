> **Superseded 2026-09-17** by [emissions-plan-2026-09-17.md](emissions-plan-2026-09-17.md), the single merged plan (this document plus the artifact review). Kept for the record; do not extend it further.

# Tret emissions accuracy: implementation and validation plan

**Date:** September 15, 2026. **Updated:** September 17, 2026. **Status:** proposed; code findings originate from the September 15 audit, with selected API and standards sources refreshed September 17. Product changes are not implemented. This update is a plan revision, not a new production audit.

**Objective:** improve physical accuracy and comparability of Tret’s AI-inference footprint estimates, while preserving historical results and making unmeasured components visible.

Companion: [research and gap analysis](/Users/williamparish/Projects/voiz/Tret/bench/docs/research/emissions-accuracy-research-2026-09-15.md).

## Decision

Keep the existing factor-resolution and history infrastructure. Introduce an explicit contract for energy boundaries and evidence, correct source calibration, and validate a deployment-aware estimator against measured workloads. Adopt newer coefficients only after matching units, boundaries and workload conditions.

### Review decisions

Accepted: correct the Jegham boundary, capture reasoning and upstream/geography evidence, evaluate regional datasets for verified deployments, and keep operational and lifecycle accounting separable. Deferred pending defensible evidence: training allocation, hosted-provider lifecycle totals, and water reporting (in its own physical units rather than as carbon). Rejected: a generic `serving_overhead = 1.7`, a production embodied estimate of 25% of operational emissions, geography inferred from provider brand or empty tables, unknown values treated as zero, and fitting unlike per-prompt medians and fixed-token observations as though they shared a denominator.

The plan makes no forecast that the corrected method will be 2–4× lower or have narrower uncertainty. Equal token counts do not establish causal energy savings, and an empty factor dataset does not establish operator configuration.

## Delivery sequence

Effort ranges are planning estimates for focused engineering work, excluding hardware procurement, supplier delays and benchmark compute costs. Do not treat them as commitments.

| Phase | Work | Indicative effort | Exit condition |
|---|---|---|---|
| 0 | Workload inventory and reference fixtures | 2–3 days | Dominant deployment/workload mix known; source observations pinned |
| 1 | Correctness, boundary contract and telemetry plumbing | 1–2 weeks | No silent boundary conversion, double PUE, ambiguous measured scenarios or whole-segment upstream misattribution |
| 2 | Measurement and factor evidence | 1–2 weeks | Reference workloads reconcile to meters; regional data covers verified deployments |
| 3 | Fit and evaluate estimator candidates | 1–2 weeks after data is available | Held-out results support a new method or retaining the corrected fallback |
| 4 | Task comparison, uncertainty and shadow rollout | 3–5 days | Comparable task metrics, validated coverage and version-aware release |

Dependencies are explicit: A1/A2 enable A3/A4; A5/A5a start early so observations exist before calibration; A6/A7 feed A9/A10. A11 task accounting and uncertainty remain core work. Broader lifecycle coverage under A8/A11a can follow separately; the boundary statement must still disclose omissions from the first release. A useful first release corrects A1–A4 while A5/A5a telemetry lands in parallel. Full calibration waits for representative measurements rather than assigning coefficients prematurely.

## Phase 0 — Establish what matters in Tret

### A0. Workload inventory

From an authorized local or production usage export, aggregate the recent representative window by model revision, provider, deployment, harness, input/output/cache counts, duration, retry count and measured/estimated status. Identify which workloads dominate both requests and current estimated energy; neither ranking alone is sufficient.

Deliver a coverage table identifying unknown reasoning tokens, upstream routing, execution regions, meter boundaries and tool activity. Use identifiers and aggregate usage; prompt content is unnecessary for the inventory.

**Acceptance:** totals reconcile to the export, incomplete usage is counted, and the denominator/window is explicit. Production access is not required to begin the correctness work below.

### A1. Pin calibration evidence

Create a reproducible source manifest for the original Jegham version/table, source PUE values, units and inference conditions. Preserve original observations and boundary-normalized observations separately. Add benchmark manifests for later ML.ENERGY/AI Energy Score imports. Research candidates include the Microsoft/Oviedo Joule study ([arXiv 2509.20241v2, June 9, 2026](https://arxiv.org/abs/2509.20241)), whose workloads are modeled rather than observations of Tret's deployment; the [SCI for AI provider/consumer boundary](https://greensoftware.foundation/articles/sci-ai-specification-ratified-standard-for-measuring-ai-emissions-across-the/); and the [Google lifecycle study](https://arxiv.org/html/2508.15734v1), which includes embodied emissions. Candidate status does not make any source a calibration input.

For every imported source, record GPU, node and facility boundaries; idle treatment; PUE; embodied treatment; input and output denominators, including thinking/reasoning; workload definition and statistic (mean, median, IQR); hardware, serving engine and batching; version/date; and license/access conditions. Record code and data licenses separately. Do not combine a generic median-per-prompt observation with a fixed 1,000-input/1,000-output-token observation as if they measured the same functional unit.

**Acceptance:** a reviewer can regenerate coefficients from immutable inputs. No paper update silently changes an existing coefficient. Every imported row has the metadata above, its statistic and denominator remain visible, and all values match the cited version before use. No unverified paper identifier or magnitude enters the manifest.

## Phase 1 — Correctness before new coefficients

### A2. Define the energy and factor contract

**Primary owners:** [emissions.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py), [emission_factors.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emission_factors.py), [energy_meter.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/energy_meter.py).

Introduce versioned metadata such as:

```text
method_id, method_version, calibration_id
energy_boundary: gpu | node_it | facility | partial | unknown
included_components, excluded_components
energy_source_by_component: measured | modeled | supplied | unknown
device_ids, measurement_interval, allocation_method, sensor_coverage
factor_boundary: generation | upstream | lifecycle | unknown
gas_coverage, gwp_basis, electricity_mix_basis
accounting_basis, region, period, dataset_version, evidence_type
```

Names are a design sketch; use the existing model conventions when implementing. Keep physical system boundaries distinct from GHG Scope 1/2/3 categories.

**Acceptance:** a GPU reading cannot be represented as complete IT energy; a facility reading cannot receive another PUE multiplication; a whole-node reading is not summed with its own GPU submeter. Existing historical rows retain their recorded numbers and get an explicit legacy/unknown boundary when necessary. Unknown component emissions remain distinguishable from verified zero.

### A3. Refit the ladder with compatible boundaries

Interpret the cited source before transforming it. [Jegham v1 Equation 1](https://arxiv.org/html/2505.09598v1) already combines GPU energy, non-GPU components and PUE. Normalize its observations to node IT before fitting, retain the original as a named legacy methodology, and do not automatically apply `serving_overhead = 1.7` or reclassify the result as accelerator-only. A Google factor of 1.72 is facility-inclusive and cannot be multiplied and then receive PUE again. Evaluate a constrained nonnegative fit and a fixed-overhead term; do not assume a two-parameter fit to three observations is robust. Treat XL interpolation and extrapolated current model assignments explicitly as weak evidence.

Distinguish a GPU-only meter from the incomplete `active_params` strategy, which still needs GPU-count and host-energy terms. Neither may silently occupy the complete-IT slot. Jegham can also omit idle energy on unassigned GPUs; inclusion of host terms does not imply full fleet coverage.

**Acceptance:** fixture calculations demonstrate the source-to-IT conversion and exactly one deployment PUE application. Include cross-provider anchors, measured override cases, and multi-GPU cases that account for GPU count and host energy before claiming complete IT coverage. Publish coefficient changes as methodology effects, without rewriting historical runs or calling them avoided real emissions.

### A4. Correct measured scenarios and baseline factor resolution

**Owners:** [analytics.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/api/analytics.py) and accounting metadata.

Define whether legacy `estimated` describes the total carbon estimate or the energy component. Add clear component-level provenance. Give what-if recomputation explicit modes: retain observed energy and change compatible factors, or estimate both comparison sides from tokens. Keep raw measured values immutable.

Resolve the baseline’s own factor set through the same layers as the actual run, keyed to its model/provider and scenario time. The current baseline path bypasses workspace/managed/harness factor resolution. Preserve intentional differences such as a different serving region, and report both sides’ provenance.

**Acceptance:** an identity scenario returns identical values for a measured run in the preserve-measurement mode; factor-only changes cannot invent a different meter reading; mixed segments preserve their own source types. Missing catalog entries and scan truncation affect both sides consistently.

For a different-model baseline, tests must demonstrate that workspace/managed PUE, model energy overrides, strategy and region/hourly grid factors resolve correctly. The existing same-model zero special case is not sufficient validation.

### A5. Resolve provider usage semantics

**Owners:** provider usage types/adapters under [providers](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/providers) and [harness.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/engine/harness.py).

Retain raw usage and normalize disjoint energy buckets. Add a nullable reasoning/thinking count plus provider-specific semantics: it may be a subset of billed output or an additional count. Anthropic exposes `usage.output_tokens_details.thinking_tokens`, including the final `message_delta`, in its [extended-thinking usage contract](https://platform.claude.com/docs/en/build-with-claude/extended-thinking); OpenAI-compatible providers may expose `completion_tokens_details.reasoning_tokens`. Pin API versions in fixtures rather than assuming universal availability. Preserve provider totals and cost behavior. Absence is unknown, not zero, and failure to read a split does not mean those tokens were omitted from the total.

Capture reasoning effort as metadata where available. Audit retry/failure behavior and whether partial work is omitted when usage is unavailable. Test the reasoning-class double-counting hypothesis by establishing whether calibration output counts represent visible text or all generated tokens. A coefficient fitted per visible token may already absorb hidden work; applying it to all generated tokens can count that work again. Refit against compatible denominators and deployment conditions before changing R; neither an 8× error nor a collapse toward another class is established.

**Acceptance:** fixtures cover (1) output including reasoning, (2) separately additional reasoning, (3) reasoning not reported, (4) input including cached tokens, (5) providers with separate cache counts and (6) older provider responses without detail fields. Final and cumulative streaming events are idempotent; thinking plus visible output reconciles according to the pinned provider semantics; every known token is counted once; costs and existing totals remain unchanged. Missing usage is visible. Do not apply a reasoning multiplier fitted to hidden work after separately counting that same work.

### A5a. Capture per-call upstream and inference geography

**Owners:** canonical usage/provider adapters under [providers](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/providers), [harness.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/engine/harness.py), and segment/accounting persistence.

Add per-call nullable `served_by` and `inference_geo` metadata without changing endpoint routing or provider request settings. The current canonical usage records only input/output/cache; `ModelSegment.served_by` records the last completed upstream turn; and factor resolution uses the catalog provider. If one model segment contains calls to multiple upstreams, split its accounting segments or persist per-call accounting records. Never apply the final `served_by` value to the whole segment.

Anthropic documents a global default, explicit `us`, and response `usage.inference_geo` in its [data-residency contract](https://platform.claude.com/docs/en/manage-claude/data-residency). Missing geography remains unknown; `global` is not a zone; `us` is not a precise regional-grid pin; provider identity is not region; and data residency at rest does not establish inference location. Preserve the evidence class for observed, configured, provider-asserted and fallback values. Resolve the upstream within the existing factor-layer precedence so explicit overrides still win and unknown upstream identity takes the documented fallback.

**Acceptance:** fixtures cover an upstream change within one run, unknown metadata, layer precedence, and old historical rows. Each call or split segment receives only its own upstream/geography evidence, and instrumentation produces no routing behavior change.

## Phase 2 — Establish trustworthy observations

### A6. Validate local metering and allocation

Prefer supported NVML cumulative energy counters where validated; keep sampled-power fallback with timing/coverage diagnostics. Add CPU/RAM/other-node measurement or labeled estimates and compare against a node/PDU reference. Use an adapter interface so the method can support externally supplied observations without embedding a particular library in the accounting model.

Measure warmup, steady state, idle reservation, concurrency and model switches separately. A single service collector should allocate a shared device’s energy once, rather than each overlapping request claiming all device energy. Allocation weights must reconcile to the measured pool, with idle/reserved work accounted for explicitly.

**Acceptance:** the sum of allocated task energy plus unallocated/reserved categories matches the source energy window within the validated instrument tolerance. Record tolerance empirically. Handle resets, negative deltas, nonfinite values, unsupported devices and gaps. Missing GPU/CPU observations must not be silently converted to zero.

### A7. Activate regional factor data for verified deployments

**Owners:** [grid_zones.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/grid_zones.py), [grid_tables.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/grid_tables.py), [grid_regions.py](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/grid_regions.py), [managed factor data](/Users/williamparish/Projects/voiz/Tret/tret-cloud/tret_cloud/emissions/managed_factors.json).

First map the regions actually needed by verified deployments, then assess Electricity Maps plus Ember, eGRID and Google regional data as candidates. Do not begin with a six-source importer rebuild. Use the existing offline import path for the selected source. Review code and dataset licensing separately before bundling: an Apache-licensed importer does not grant rights to its data, and ODbL is not automatically a blocker, but its obligations must be recorded separately ([ODbL 1.0, §2.3](https://opendatacommons.org/licenses/odbl/1-0/)).

Keep direct generation and life-cycle factors distinguishable, with provenance and observation years. Resolve actual upstream provider/region only from configuration evidence or supplier metadata. An empty table does not prove production defaults, and provider brands do not justify defaults such as Anthropic-US or Mistral-France. Establish the correct citation and boundary before choosing a number; no value such as 473 gCO2e/kWh is mandated by this plan. Unknown hosted regions keep a labeled fallback/scenario range.

**Acceptance:** a supported pin resolves to a dated compatible source; an unsupported pin reports its fallback; typical-day profiles and actual dated observations have distinct metadata; long runs integrate energy across applicable intervals. No new live network dependency is introduced into run execution.

### A8. Improve embodied coverage

Define whether `runs_over_lifetime` means requests or batches before retaining the current divisor. Prefer equipment footprints allocated by time and reserved resource share, with sensitivity to lifetime assumptions. Add cloud hardware as a separately supplied/estimated component when evidence permits; do not force it to zero merely because the customer does not own the equipment. Manufacturing allocation is independent of the runtime grid factor. A heuristic such as `embodied = 0.25 × operational` may appear only as an explicit, low-confidence scenario and never as a production estimate.

**Acceptance:** allocating a complete equipment lifetime recovers its total footprint once; batching cannot divide the same workload twice; changing the runtime grid factor leaves embodied emissions unchanged; supplier totals already containing hardware, including applicable Google lifecycle totals, do not receive another hardware addition. Unsupported components remain excluded/unknown with visible coverage. Keep amortized service footprints separate from corporate capital-goods reporting.

## Phase 3 — Build a calibration experiment

### A9. Benchmark design

Use Tret’s task distribution to select workloads. A starting matrix is:

| Dimension | Initial coverage |
|---|---|
| Input length | Short, typical, long-context, and the real upper tail |
| Output length | Short response, normal response, long generation |
| Reasoning | Off/low/high where supported; preserve emitted usage semantics |
| Cache | Cold prefill, warm prefix, mixed hit rates |
| Serving | Single request plus representative concurrency/batch levels |
| Deployment | Dominant local configurations; hosted only where useful evidence exists |
| Hardware/software | Exact model revision, GPU/count, precision, engine version |
| Operational conditions | Warmup, steady state, idle periods, retries and failed tasks |

Start with a screening subset instead of multiplying every axis into an unaffordable full factorial experiment. Increase repetitions until the variance and instrument behavior are understood. Capture aggregate windows long enough to measure short calls reliably, while retaining request traces for allocation.

Compare three candidates: corrected ladder; nonnegative prefill/decode/context model; architecture-aware model with completed GPU/host boundaries. Evaluate energy first, then apply a fixed compatible grid factor to isolate energy-model errors from carbon-factor differences.

### A10. Pre-register validation criteria

Use separate fit, validation and held-out sets; hold out prompt shapes and serving configurations, not only randomly selected near-duplicate requests. Compare signed aggregate bias, median absolute error, weighted absolute percentage error, tail error, interval coverage and failures by workload subgroup. Avoid MAPE for near-zero readings.

**Proposed initial targets, subject to reference-meter precision:** aggregate energy bias within ±10% and weighted absolute percentage error at or below 20% for supported, measured deployment classes. These are engineering targets, not promises or requirements from a standard. Require no material regression against the corrected ladder on important workload groups. Publish errors for long context, cache hits and reasoning separately.

Opaque hosted APIs cannot earn the same accuracy label without validation evidence. Retain wide scenarios/fallback status there. If the richer model fails held-out validation, ship the corrected ladder and improved coverage instead.

## Phase 4 — Useful comparisons and controlled release

### A11. Task-level accounting and uncertainty

Build a reconciled activity tree for main inference, routing, compaction, child runs, retries and material non-LLM work, reusing existing `Run.conversation_id` attribution where it helps. Define accepted-task quality and latency gates. Report absolute Wh/g totals, Wh/g per accepted task, success rate and missing coverage. For a fixed evaluation cohort, divide all attributable energy/emissions, including failed attempts and retries, by the number of accepted deliverables. Failed attempts do not increase that denominator. If none succeed, report the rate as undefined alongside the total and failure count.

Replace evidence-by-label narrowing with evidence classes tied to real validation. Propagate uncertainty using correlated factor scenarios; use distributions only where evidence supports them. Model omissions separately from uncertainty around included components. Preserve location/market/consequential views and withhold incompatible totals.

**Acceptance:** the same failed work cannot vanish from both the numerator and coverage record; duplicate child/run accounting is detected; comparison cohorts satisfy the same quality gate and system boundary. A method change is shown separately from an operational improvement.

### A11a. Publish a boundary and conformance matrix

Define separate deliverables for an operational inference estimate, a full lifecycle inference result, a provider model-development result and an SCI for AI consumer score. The [SCI for AI specification](https://github.com/Green-Software-Foundation/sci-ai/blob/dev/SPEC.md) distinguishes provider development/training from consumer operation/monitoring and includes embodied hardware in its consumer calculation; therefore a consumer score is not a synonym for an electricity-only estimate, and training is not automatically amortized into every inference result. Pin the ratified revision before claiming conformance because the linked development text can move. Keep market-based inventory views separate from SCI results where their rules differ.

Publish a scope/coverage matrix instead of a blanket compliance claim. Add training allocation only when the chosen boundary requires it and the data is defensible. Report water separately in appropriate physical units. The [GHG Protocol FAQ dated July 29, 2026](https://ghgprotocol.org/blog/ghg-protocol-announces-key-standard-development-updates-faq-resource) describes a Q2 2027 consultation and planned Q4 2028 publication of a consolidated corporate standard; do not present proposed revisions as current requirements.

**Acceptance:** every result names its functional unit, lifecycle stages, included/excluded components and evidence coverage; failed work is counted consistently; conformance claims cite a pinned standard revision and only the covered boundary. A first product release may improve operational accuracy without waiting for water or training accounting.

### A12. Shadow release and review

Run the proposed method alongside legacy accounting for a representative window. Store both with method versions, compare ranking reversals and error by workload, and investigate unexpected changes. Keep the historical default view as-recorded; offer explicitly labeled restated estimates. Provide rollback to the previous calculation version.

Before merging, review the combined code/data diff and update [emissions-methodology.md](/Users/williamparish/Projects/voiz/Tret/bench/docs/emissions-methodology.md), model comments and user-facing claims. Reuse existing tests, adding only cases that exercise new contracts or actual failure modes.

**Acceptance:** traceable factors for every result; supported-domain accuracy demonstrated; missing components visible; all comparisons use compatible boundaries; no production release claim relies only on passing unit tests.

## Verification starting point

The September 15 research audit ran these existing tests: **209 passed**. They were not rerun for this September 17 documentation update, and the result is historical rather than evidence of current production state.

```sh
cd /Users/williamparish/Projects/voiz/Tret/bench/backend
.venv/bin/pytest -q tests/test_emissions.py tests/test_energy_meter.py tests/test_emissions_whatif.py tests/test_emission_factors.py
```

Extend with the existing strategy, phase-3, settings, grid and integration tests as affected. Cloud tests were blocked by a missing `stripe` dependency in the September 15 test environment; recheck that environment before implementation validation. Re-run the relevant checks after implementation, then review the combined diff before merge/deploy.

## Recommended first work package

Implement **A1–A4** first: reproducible calibration evidence, explicit energy boundaries, source-PUE normalization and trustworthy measured-scenario behavior. Start A5/A5a telemetry in parallel, without calibrating prematurely, and run the workload inventory alongside it. This creates a reviewable first release while the measurement and regional evidence needed by A9/A10 accumulates. Keep broader lifecycle expansion under A8/A11a separately labeled; retain A11 task accounting and uncertainty in the core plan.
