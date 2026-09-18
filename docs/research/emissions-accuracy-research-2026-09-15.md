# Tret emissions accuracy: research and gap analysis

**Research date:** September 15, 2026. **Priority:** improve calculation accuracy.

**Scope:** the energy and greenhouse-gas footprint of AI inference routed through Tret, including local serving and hosted APIs. This is not a review of the climate-risk calculations inside generated customer reports.

**Evidence:** local source inspection plus primary research, benchmark documentation, supplier methodologies and accounting guidance. The checked-out code is evidence of implemented behavior, not proof of which settings are active in production. No production usage export, hardware measurements or supplier-specific request telemetry was supplied. Recommendations below are proposed engineering decisions, not implemented changes.

Read the [implementation plan](/Users/williamparish/Projects/voiz/Tret/bench/docs/research/emissions-accuracy-plan-2026-09-15.md) for work packages and acceptance criteria.

## 1. Conclusions

Tret has useful infrastructure for explaining estimates: factor provenance, stored historical calculations, configuration layers, caveats, and comparisons. The highest-value improvement is to make the physical boundaries and evidence behind every number consistent.

1. **Correct the calibration boundary first.** The Jegham source behind the default energy ladder already includes PUE, the ratio of facility energy to IT energy. Tret treats its fitted coefficients as IT energy and applies PUE again. Normalize the original observations before refitting; do not simply substitute a newer global constant.
2. **GPU measurement is not whole-server measurement.** Automatic NVIDIA telemetry leaves CPU, RAM and other server loads unmeasured, yet enters the same slot as whole-IT energy. Shared GPUs can introduce the opposite error by attributing unrelated work to a run. Neither direction can be assumed to dominate.
3. **Replace universal token weights progressively.** Actual work depends on prefill, generated tokens including reasoning, growing context/KV cache, hardware, precision, serving engine, batch size and utilization. A model name or price tier cannot capture these reliably.
4. **Separate missing data from zero.** Unknown cloud hardware emissions, missing serving regions, and unreported work should contribute explicit coverage gaps. They do not establish a zero footprint.
5. **Validate against completed tasks.** A fixed-token comparison is useful for a controlled scenario. It cannot establish the savings from routing real tasks when response lengths, retries and success rates differ.

There is no defensible universal “emissions per AI prompt” constant. Tret should retain a transparent fallback while adding stronger, deployment-specific methods where evidence permits.

## 2. What Tret calculates today

The default calculation, before measured-energy overrides and other optional features, is:

```text
weighted_tokens = 0.05 × input + output
                + 0.005 × cache_read + 0.05 × cache_write

energy_wh       = class_wh_per_million × weighted_tokens / 1,000,000
facility_wh     = energy_wh × PUE
electricity_g   = facility_wh × grid_g_per_kwh / 1,000
reported_g      = electricity_g + included_embodied_g
```

The S/M/L/XL/R ladder is 250 / 1,200 / 2,600 / 6,000 / 21,000 Wh per million output-equivalent tokens. These are coarse estimates fitted or interpolated from historical model observations, not direct measurements of each catalog model. The default grid factor is 470 g/kWh, represented as CO₂e in Tret. The usual uncertainty band divides/multiplies the central estimate by 2.5; it is a judgment band, not a statistical confidence interval.

PUE defaults are 1.2 for cloud, 1.05 for workstation and 1.56 for on-premises. For an illustrative M-class cloud call with 1,000 fresh input and 1,000 output tokens and no cache, the current arithmetic produces 1,050 weighted tokens, 1.26 nominal IT Wh, 1.512 facility Wh and 0.71064 g at the 470 factor, before embodied emissions. This is a worked example of existing arithmetic, not a measurement or recommended coefficient.

The input/output split improves on counting all tokens equally. But a cache-price discount is not empirical evidence of an identical energy discount, and a universal 20:1 output/input ratio cannot capture different architectures, prompt shapes and serving conditions.

### Existing capabilities to build on

| Capability | Present behavior | Remaining accuracy work |
|---|---|---|
| Factor provenance and layers | Records sources, dates, labels and winning configuration layers | Add physical boundaries, versioned datasets and evidence quality |
| Per-model energy overrides | Can replace the ladder with a supplied coefficient | Require calibration conditions and applicability limits |
| Active-parameter strategy | Implements an EcoLogits GPU-energy expression | Complete multi-GPU and host accounting; validate the deployment assumptions |
| Automatic/external energy input | NVIDIA power sampling and supplied Wh | Identify sensor boundary, allocation and measurement coverage |
| Regions and hourly grid tables | Operator-pinned regions and pasted time series | Verify serving location; distinguish actual hourly observations from typical-day profiles |
| Regional dataset importer | Supports offline imported grid data | Bundled zone table is empty in the inspected checkout |
| Managed supplier factors | Extension layer exists | Shipped cloud supplier map is empty |
| Embodied hardware | Flat local allowance or hardware profile | Replace weak defaults; clarify denominator and cloud coverage |
| Adaptive uncertainty | Optional evidence-driven narrowing exists | Labels/dates alone do not prove measurement quality; missing components remain missing |
| Historical rollups | Preserve stored calculations and separate mixed accounting bases | Keep boundary/version/coverage separation too |
| What-if scenarios | Recalculate token estimates under scenario factors | Separate this from re-carbonizing measured energy |

The [existing methodology](/Users/williamparish/Projects/voiz/Tret/bench/docs/emissions-methodology.md) documents many of these limitations. Some older sections say that nothing is metered even though later sections describe metering. Code behavior and source evidence take precedence over those statements.

## 3. What current methods do differently

### A. Measure an identified system, then allocate it to work

NVIDIA NVML provides GPU board power and, on supported devices, cumulative GPU energy. It does not establish full server power or request ownership. Cumulative energy differences can avoid some errors of integrating sparse instantaneous samples, but still need device identification, reset handling and validation. [NVIDIA NVML reference](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html), [NVML overview](https://developer.nvidia.com/management-library-nvml).

CodeCarbon combines GPU telemetry with CPU measurement or estimation and RAM estimation. Its documentation explicitly exposes fallback methods; “tracked” energy is not necessarily metered for every component. This makes it a useful integration candidate, with component-level provenance, rather than an automatic accuracy certification. [Current methodology](https://docs.codecarbon.io/latest/explanation/methodology/), [configuration and GPU attribution limits](https://docs.codecarbon.io/latest/how-to/configuration/).

TokenPowerBench, initially published in December 2025, distinguishes GPU, node and system measurement and aligns energy with prefill, decode and idle phases. Its approach is useful for building a validation harness that compares component telemetry to node or rack instruments. Node measurements still need a separate facility-overhead boundary. [Paper](https://arxiv.org/abs/2512.03024), [authors’ implementation](https://github.com/chenxuniu/TokenPowerBench).

**Tret recommendation:** prioritize a measurement contract and resource allocation before replacing the meter library. Prefer a validated node/PDU meter for ground truth. Where only GPU telemetry is available, publish that component plus separately identified estimates for the rest.

### B. Calibrate on matching workload and deployment conditions

ML.ENERGY v3, released in December 2025 and described in January 2026, expands to 46 models, seven tasks and 1,858 configurations on H100 and B200 hardware. Its GPU benchmarks show why reasoning cannot be represented by a single fixed multiplier: output length and batching constraints both change. These are controlled deployment observations, not universal hosted-API coefficients. [Official v3 analysis](https://ml.energy/blog/measurement/energy/diagnosing-inference-energy-consumption-with-the-mlenergy-leaderboard-v30/).

A February 2026 longitudinal analysis found 15–41% lower energy per token in its Llama comparisons as the serving stack evolved. The authors also identify changes in measurement windows, and the 405B comparison changes precision. This supports versioning calibration data; it does not justify applying those percentage reductions across Tret’s catalog. [Authors’ longitudinal analysis](https://ml.energy/blog/measurement/energy/llm-inference-energy-a-longitudinal-analysis/).

AI Energy Score v2 added reasoning benchmarks in December 2025. Its official methodology distinguishes GPU energy scores from fuller system energy and uses standardized workloads. Task scores, batch settings and hardware must travel with imported measurements. Relative star ratings should not become Wh coefficients. [V2 release](https://huggingface.co/blog/sasha/ai-energy-score-v2), [methodology](https://github.com/huggingface/AIEnergyScore/blob/main/index.md).

**Tret recommendation:** use raw, compatible energy observations. Store model revision, precision, engine version, GPU configuration, prompt/output distributions and concurrency. Hold out Tret-like workloads for validation rather than fitting and judging on the same benchmark rows.

### C. Use physical models where deployment inputs are known

EcoLogits distinguishes active parameters, which affect compute, from total parameters, which affect model memory and required GPU count. Its pipeline includes GPU multiplicity, host energy and hardware allocation. Tret’s existing active-parameter path imports only part of that accounting. The documented fixed batch assumption and training domain of the fit remain limitations even after adding the missing terms. [EcoLogits inference methodology](https://ecologits.ai/latest/methodology/llm_inference/).

The July 29, 2026 preprint *From Tokens to Watt-hours* models prefill and decode using computation and memory traffic, including KV-cache effects, on H100-class GPUs. Its boundary excludes the host and facility. It is a promising candidate for evaluation; peer-reviewed validation and portability across Tret’s deployment mix were not established by this research. [Paper and submission date](https://arxiv.org/abs/2607.26571).

**Tret recommendation:** evaluate a nonnegative prefill/decode model against the corrected ladder. Add architecture detail only when it improves held-out prediction. Guessed parameter counts, batch sizes and hardware should generate scenarios, not a precise “measured” result.

### D. Supplier disclosures show why boundaries matter

Google’s August 2025 study reports 0.24 Wh for the median Gemini Apps text prompt in May 2025. A narrower approach produces 0.10 Wh; the difference includes both boundary and utilization sampling changes. Its carbon result uses market-based electricity accounting. These values should not be compared directly with a location-based, GPU-only or differently sized Tret request. [Google technical paper](https://arxiv.org/html/2508.15734v1).

Google Cloud’s customer methodology also allocates shared infrastructure and embodied equipment emissions. Those records could support reconciliation for workloads actually covered by a customer’s cloud account; they do not automatically reveal the footprint of arbitrary third-party APIs. [Google Cloud methodology](https://docs.cloud.google.com/carbon-footprint/docs/methodology).

Mistral’s July 2025 environmental study includes greenhouse gases, water and resource depletion, with upstream manufacturing included in its inference discussion. It demonstrates broader life-cycle coverage, but its specified 400-token response and methodology are not interchangeable with Google’s median prompt. [Mistral disclosure](https://mistral.ai/news/our-contribution-to-a-global-environmental-standard-for-ai/).

## 4. Highest-priority accuracy gaps

### G1. PUE is embedded in the calibration source and applied again

**Finding:** Jegham v1’s Equation 1 explicitly multiplies modeled GPU and non-GPU power by PUE. The observations used in Tret’s regression are therefore already facility-adjusted estimates. Table 1 assigns PUE 1.12 to OpenAI anchors and 1.14 to the Sonnet anchor. [Original May 2025 paper, sections 4.1–4.2](https://arxiv.org/html/2505.09598v1).

**Consequence:** a coefficient retaining source PUE is mislabeled when used as IT Wh. Applying the deployment PUE then includes overhead twice. The precise effect on shipped rounded classes requires refitting; other estimation errors can be much larger.

**Fix:** pin the source version and observation table, divide each source observation by its source PUE, then refit IT coefficients. Alternatively preserve a facility-bound coefficient and explicitly convert between facility conditions. Do not remove PUE globally: GPU and node measurements still require appropriate conversion. Publish this as a methodology correction, not a real-world emissions reduction.

As a diagnostic, I reproduced ordinary least squares from Tret’s stored rounded observations. Normalizing source PUE gives the following output coefficients; these are **not proposed production replacements**:

| Anchor | Reproduced original output coefficient, Wh/M output tokens | Source-PUE-normalized coefficient | Fit limitation |
|---|---:|---:|---|
| GPT-4.1 nano | 271.9 | 242.7 | Input coefficient near zero |
| GPT-4o | 1,233.1 | 1,101.0 | Input coefficient remains negative |
| Claude 3.7 Sonnet | 2,634.7 | 2,311.1 | Only three observations |
| o3 | 20,850.4 | 18,616.5 | Only three observations |
| DeepSeek-R1 | 35,474.8 | 27,932.9 | Input coefficient remains negative |

Dividing source energy by PUE is necessary for boundary consistency; it does not repair the degenerate fits, source hardware assumptions, or extrapolation to current models. XL has no direct anchor to normalize independently.

### G2. The measured path conflates sensor coverage with complete energy

**Finding:** NVIDIA sampling measures GPU devices. CPU, RAM, storage, networking, fans and PSU losses are not all included. Shared-device readings can include other requests or processes. PUE accounts for facility overhead; it does not fill in missing server components.

**Fix:** persist `measurement_boundary`, component coverage, device IDs, wall-clock interval, sensor method, sample coverage, shared-device status and allocation method. Reject unsupported “whole IT measured” claims. A scalar external Wh input also needs a declared boundary.

**Validation:** reconcile component measurements to a node meter, including idle and concurrent traffic. Test sub-second calls, partial sensor failures, device resets and multi-GPU serving. Do not accept a ±10% total-footprint band solely because one energy component was measured.

**Related metadata issue:** the accounting block sets `estimated: true` even when `energy_source: measured` is returned. Carbon can still be estimated when energy is measured, so define the two meanings explicitly and use component-level status instead of letting consumers guess from overlapping flags.

### G3. Reasoning and cache usage need explicit semantics

**Finding:** a broad reasoning class is an imperfect stand-in for actual computation; the same model can use different reasoning effort. Providers also differ in whether aggregate token totals already contain reasoning and cached tokens.

**Fix:** preserve raw provider usage and normalize disjoint energy buckets with an explicit inclusion contract. Track fresh prefill, cached prefix, visible generation and additional hidden generation where exposed. Record “unknown” for unreported reasoning. If output already includes reasoning, do not add it again. Recalibrate any reasoning premium when explicit reasoning work is counted, avoiding a second premium for the same computation.

**Cache detail:** KV-cache hits avoid repeated prefill, but cache fetch, retention and attention over the cached context are not free. Cache price ratios are commercial terms. Fit energy effects using cache-on/off measurements, context length, hit ratio and retention conditions.

### G4. Regional support exists, but shipped data and serving evidence are incomplete

**Finding:** the checked-in regional table and managed cloud supplier entries are empty. Region pins and hourly CSVs are supported, but a configured region is not proof that a multi-region API actually served the request there. A repeating 24-hour profile is not a historical measurement of a specific date.

Electricity Maps distinguishes consumption-based electricity, including cross-border flows, from local production and offers temporal data. Its life-cycle factor coverage also differs from generation-only factors. [Data methodology](https://www.electricitymaps.com/data/methodology), [life-cycle explanation](https://ww2.electricitymaps.com/methodology).

**Fix:** populate an offline, versioned dataset for verified deployments and retain a clearly labeled unknown-region fallback. Add actual timestamps and energy-weighted integration for work crossing factor intervals. Follow the existing offline-data architecture; do not introduce a live network dependency into request execution.

### G5. A newer number is insufficient without a compatible factor definition

IEA’s *Electricity 2026* estimates world generation intensity at **435 g CO₂/kWh for 2025**. That is an estimated historical generation-CO₂ figure, not a 2026 measurement or a complete life-cycle CO₂e factor. Its 2030 value is a forecast. [IEA emissions analysis](https://www.iea.org/reports/electricity-2026/emissions).

**Fix:** review Tret’s 470 default and citation together. Record gas coverage (`CO2` versus `CO2e`), global-warming-potential basis when applicable, generation/life-cycle boundary, production/consumption mix, year, observed/estimated/forecast status, geography and accounting basis. Do not automatically replace 470 with 435 inside an otherwise unchanged “life-cycle CO₂e” field.

### G6. Scope labels and footprint boundaries answer different questions

For purchased API services, Scope 3 Category 1 is generally the relevant customer-inventory category; organizational control and contractual arrangements still matter. The physical service footprint can include operational electricity and embodied hardware regardless of customer ownership. [GHG Protocol purchased-services calculation guidance](https://ghgprotocol.org/sites/default/files/2022-12/Chapter1.pdf).

Scope 2 uses generation emissions, excluding upstream electricity life-cycle emissions and transmission/distribution losses; those need separate treatment. Assigning an entire life-cycle electricity factor to a local run’s Scope 2 misclassifies components. [Final Scope 2 Guidance, chapters 4–5](https://ghgprotocol.org/sites/default/files/2023-03/Scope%202%20Guidance.pdf).

**Fix:** calculate physical components first and map them to an explicitly named reporting entity second. Preserve separate location-based, market-based and consequential estimates. Local/cloud is not sufficient to infer every ownership or lease arrangement. An unmodeled backup generator or refrigerant source should not establish a measured Scope 1 zero.

**Standards status:** GHG Protocol’s 2026 FAQ now describes a consolidated consultation in Q2 2027 and publication in Q4 2028. Older pages forecasting a final Scope 2 update in 2027 are superseded on timeline. Proposed revisions should be marked as proposals, not implemented requirements. [Current development FAQ](https://ghgprotocol.org/blog/ghg-protocol-announces-key-standard-development-updates-faq-resource).

### G7. Embodied allocation needs a defensible denominator

Tret supports local hardware profiles, but cloud hardware remains excluded and the hardware constants are weak. A lifetime-run count must specify whether it counts individual requests or batches. Dividing by both lifetime requests and batch size would allocate too little. Request lengths and reserved idle capacity further complicate equal-per-run allocation.

**Fix:** use documented equipment footprints and a time/resource allocation where possible. Identify lifetime, reserved share, GPU count, chassis share and utilization assumptions. Prevent separately adding hardware already included in a supplier footprint. Keep amortized product/service allocation distinct from corporate capital-goods inventory treatment.

Software Carbon Intensity provides a useful design reference: an explicit system boundary, a functional unit, operational electricity and an allocated share of hardware emissions. It includes reserved resources and supporting infrastructure within its chosen boundary. [SCI specification](https://sci.greensoftware.foundation/).

### G8. Uncertainty should reflect error and incompleteness

Tret’s judgment bands and caveats are preferable to false precision. However, configuring a labeled PUE is not proof it was metered, and adding a date to a grid factor does not establish its representativeness. A narrow interval around an incomplete system boundary can still be substantially wrong.

**Fix:** separate measurement error, model error, deployment uncertainty and missing-component coverage. Use observed residuals for calibrated methods; use scenario ranges when data is insufficient for a distribution. Preserve common-factor correlation in rollups. Do not impose a fixed maximum range when an unknown region or unsupported model could exceed it.

### G9. Count the workflow and compare equivalent outcomes

Inference energy is only part of an agent workflow. Inventory model retries, failures, fallbacks, routing, compaction, tool execution, embedding, retrieval/reranking, sandbox work, document processing and retained storage. Tret already accounts for some model overhead, so this is a coverage audit, not a claim that every item is missing.

**Fix:** give each attributable activity an identity and parent run; reconcile provider calls, child runs and totals without double counting. Include failed work in task cost. Compare Wh and gCO₂e **per accepted task** at a defined quality and latency threshold. Keep absolute totals too, so a lower per-task footprint cannot conceal growing total consumption.

The present same-token counterfactual should be named as such. A real routing comparison needs observed baseline tasks or a validated workload model. Average grid accounting describes allocated footprints; estimating a causal electricity change requires a separate consequential method. [Electricity Maps on marginal emissions](https://www.electricitymaps.com/resources/publications/our-latest-research-on-marginal-emission-factors).

### G10. What-if scenarios must preserve the quantity being compared

The current what-if endpoint regenerates token estimates even for measured runs. An empty or identity scenario can therefore differ from the recorded result. This is documented behavior, but an accuracy-focused user can easily interpret it as the effect of changing factors.

**Fix:** offer explicit modes: (a) retain measured energy and change only compatible carbon/PUE factors, or (b) compare two token-estimated scenarios. Preserve raw measurements. If a facility measurement already includes PUE, changing a PUE assumption alone must not silently change that observed facility quantity. Expose exclusions and keep each comparison’s population identical.

### G11. Baseline calculations bypass the layered factor resolution

The actual run uses its resolved `FactorSet`, but `_baseline_block()` resolves the other model from plain settings and legacy arguments. Its energy comes from the catalog model’s `energy_wh()` method. Workspace/managed/harness PUE, grid regions/tables, energy strategy and per-model overrides do not take the same path. The same-model comparison is special-cased to zero, so that identity test does not detect the inconsistency.

**Consequence:** for different models, “avoided” carbon can reflect differing configuration paths as well as a model choice. This is separate from the conceptual weakness of a same-token counterfactual.

**Fix:** resolve a distinct baseline factor set through the same configuration layers, using the baseline model/provider and the scenario’s declared time and deployment. Do not blindly copy the actual provider’s factors. Store both sides’ provenance and disclose intentional scenario differences. [Baseline resolution](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:2165), [baseline call site](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:2450).

## 5. Variable inventory

Priority reflects likely value to Tret’s accuracy; actual materiality depends on its workload mix.

| Variable | Why it matters | Tret status / action | Priority |
|---|---|---|---|
| Energy boundary and included components | Prevents omission and double PUE | Make explicit for every method | Immediate |
| Provider usage semantics | Prevents cached/reasoning double counting or omission | Normalize and retain raw usage | Immediate |
| Model revision and reasoning effort | Work can change under one family name | Version calibration and usage | High |
| Fresh input and output lengths | Prefill and decode have different costs | Already weighted; calibrate separately | High |
| Context and KV-cache size | Memory traffic grows during generation | Add context-conditioned calibration | High |
| GPU model/count, memory, interconnect | Changes power, speed and parallelism | Capture for known deployments | High |
| Active and total parameters, MoE structure | Compute and memory requirements differ | Partial active-parameter support | High |
| Precision/quantization and engine version | Changes throughput and memory behavior | Add calibration metadata | High |
| Batch size, concurrency, utilization | Determines shared load and request allocation | Measure effective serving conditions | High |
| CPU/RAM/other node load, idle/reserved capacity | GPU-only boundaries miss these | Measure or estimate separately | High |
| Serving region and actual run time | Determines relevant grid factor | Verify pins; populate offline data | High |
| PUE with boundary and period | Converts complete IT to facility energy | Support exists; validate evidence | High |
| Electricity gas/mix/life-cycle basis | Similar units can describe different emissions | Expand factor contract | High |
| Task success, retries, tool work | Changes work needed for the same outcome | Reconcile complete activity tree | High |
| Hardware footprint, lifetime, reserved share | Captures embodied burden | Existing local profile needs stronger allocation | Medium–high |
| Speculative decoding and rejected draft work | Billed tokens can differ from executed work | Capture locally; unknown on opaque APIs | Medium |
| Network, storage, vector databases, OCR | Can matter outside model serving | Measure material contributors | Medium |
| Training and fine-tuning allocation | Relevant to broader life-cycle claims | Separate optional component with assumptions | Boundary-dependent |
| Backup fuel and refrigerant leakage | Not included in electricity-only arithmetic | Supplier/site data where material | Boundary-dependent |
| Water consumption/withdrawal and scarcity | Different environmental impact from carbon | Separate metric; specify onsite/upstream and watershed | Later |

Water should not be added to grams of CO₂e. A fuller environmental product can report liters and scarcity-weighted impacts separately; it does not repair an inaccurate energy estimate.

## 6. Proposed calculation architecture

```text
provider usage + execution trace + deployment metadata
                          ↓
     measured components / calibrated estimates / explicit unknowns
                          ↓
         allocate shared IT and reserved capacity to task
                          ↓
         apply facility conversion exactly once
                          ↓
       multiply time/region energy by compatible factors
                          ↓
    add non-overlapping embodied and other selected components
                          ↓
       report total + per accepted task + coverage + uncertainty
```

Proposed physical calculation, using Wh and g/kWh:

```text
C_electricity_g = Σ(region, time) [E_facility_Wh × I_compatible_g_per_kWh / 1000]
C_covered_g     = C_electricity_g + C_hardware_g + C_other_included_g
```

Do not infer `E_facility` by multiplying an already facility-inclusive observation. Do not sum overlapping whole-node and GPU readings. Represent omitted components in a separate completeness record.

Suggested method selection:

1. Applicable, validated whole-node/facility observations or supplier activity allocations.
2. Deployment-specific calibration with a supported workload domain.
3. Matching public benchmark calibration with a declared boundary conversion.
4. Architecture/serving model with known inputs and explicit scenarios for unknowns.
5. Corrected class ladder as a transparent fallback.

This ordering is conditional: a mismatched supplier aggregate or contaminated meter is weaker than a well-matched calibration. Judge coverage, allocation and representativeness rather than the method’s name.

## 7. Questions that require Tret’s own data

- What percentage of calls and estimated energy is hosted versus local, and which model revisions dominate?
- Are any hosted endpoints region-pinned with evidence of the execution location and upstream provider?
- Which adapters return reasoning/cache usage, and how do aggregate fields include those counts?
- How much workflow work occurs in retries, tools, child runs, retrieval or document processing?
- Are local servers shared? What hardware, precision, engine versions and concurrency are used?
- Can a node meter or provider report establish a reference total for a bounded period?
- What outcome and latency thresholds define a successful Tret task?

These determine prioritization within the plan. They do not block correcting known boundary errors. No claimed production error percentage or accuracy improvement can be justified until the workload and reference measurements are available.

## 8. Evidence record and limits

### Verified code locations

| Finding or capability | Source |
|---|---|
| Original observation table and class coefficients | [emissions.py:104](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:104), [class ladder:153](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:153) |
| Weighted tokens and estimated energy | [emissions.py:897](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:897) |
| Grid default provenance | [emissions.py:366](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:366) |
| PUE, embodied and uncertainty defaults | [config.py:531](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/config.py:531) |
| Active-parameter missing system terms | [emissions.py:776](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:776) |
| GPU sampling and integration | [energy_meter.py:141](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/energy_meter.py:141), [integration:177](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/energy_meter.py:177) |
| Measured input, PUE conversion and provenance | [emissions.py:2329](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:2329), [PUE:2432](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:2432), [flags:2465](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:2465) |
| Scope assignment | [emissions.py:2255](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:2255) |
| Cloud embodied exclusion | [emission_factors.py:1037](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emission_factors.py:1037) |
| Regional dataset boundary | [grid_zones.py:73](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/grid_zones.py:73), [bundled data](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/data/grid_zones.json) |
| What-if estimates | [analytics.py:1187](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/api/analytics.py:1187) |
| Same-token baseline | [emissions.py:580](/Users/williamparish/Projects/voiz/Tret/bench/backend/tret/services/emissions.py:580) |
| Managed factors | [managed_factors.json](/Users/williamparish/Projects/voiz/Tret/tret-cloud/tret_cloud/emissions/managed_factors.json), [empty-layer behavior:107](/Users/williamparish/Projects/voiz/Tret/tret-cloud/tret_cloud/emissions/__init__.py:107) |

### Verification

The existing targeted core suite passed **209 tests**:

```sh
cd /Users/williamparish/Projects/voiz/Tret/bench/backend
.venv/bin/pytest -q tests/test_emissions.py tests/test_energy_meter.py tests/test_emissions_whatif.py tests/test_emission_factors.py
```

Passing tests establish implementation consistency, not scientific validity of the calibration assumptions. Cloud emissions tests could not start because the available environment lacked `stripe`; no dependency was installed to change that environment.

A separate review checked the report and plan, local link targets, worked arithmetic, reproduced calibration coefficients, and baseline-resolution finding. It found no substantive artifact issues. Runtime code and production configuration were not changed.

- Core checkout inspected: `217c5dc0055952a1f01339f5ddacad9ec1c351f9`.
- Cloud checkout inspected: `e173e04c714ce6143009e09194dbdbff9d875123`.
- The root Tret directory contains separate repositories. Existing untracked `bench/backend/uv.lock` was not part of this research change.
- Web sources were checked on September 15, 2026. Versioned papers are preferable to moving documentation URLs for future calibration imports.
- Recent papers and benchmark results show useful methods, not a validated substitute for Tret-specific measurements. This report does not assert complete coverage of all 2026 literature.
- Production configuration, real routing distribution, raw benchmark data and physical meter accuracy were not verified. Source citations support methods and boundaries; the recommended design and prioritization are this review’s synthesis.
