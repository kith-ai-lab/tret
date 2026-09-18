# Tret emissions batch: replication and audit

**Date:** 2026-09-17. **Subject:** the uncommitted implementation batch described in
[emissions-plan-2026-09-17.md](emissions-plan-2026-09-17.md) ("Implementation
progress" and "Historical validation execution") and the private results in
`validation-artifacts/emissions-2026-09-17/validation-results.md`.
**Method:** every numeric claim was re-derived independently (own scripts, not
the repo's tooling) and then cross-checked by rerunning the repo's tooling;
the full test suites were rerun; three read-only code reviews covered the
accounting core, the provider/harness/metering path, and docs/frontend/claims.
Nothing in the tree was modified by this audit.

## Verdict

**Every quantitative claim replicates.** The Jegham refit, the v2 constants,
the Ember figures, the workload inventory and the retrospective replay all
reproduce exactly from pinned inputs. The arithmetic invariants the plan
promised hold on every path the reviewers could construct: PUE applied once,
measured values immutable, what-if exclusions symmetric, unknowns withheld
rather than zeroed.

**Not ready to commit as-is.** The batch's most visible correction, "the grid
default is cited to its real source", is still mis-cited on two live
user-facing surfaces. The headline replay figure is attributed to the wrong
cause in the plan. Two shipped defaults change on upgrade with no upgrade
note, and one of them nulls existing dashboards. Local metering will
self-disable under ordinary event-loop jitter. Multi-run legacy deliverables
lose their footprint paragraph. None of these corrupts a stored number; all
are fixable in a day.

## 1. Replication results

| Claim | Source of claim | Independent result | Verdict |
|---|---|---|---|
| Jegham v1 table (5 models × 3 shapes) matches the paper | plan A1 | Paper Table 4 fetched; repo values are the paper's 3-dp means rounded to 2 dp; Table 1 PUE 1.12 / 1.14 / 1.27 match | Reproduced |
| Legacy OLS b: 271.9 / 1,233.1 / 2,634.7 / 20,850.4 / 35,474.8 | methodology doc | Exact from the repo's rounded table; within 0.6% from the paper's unrounded means | Reproduced |
| Source-PUE-normalised b: 242.7 / 1,101.0 / 2,311.1 / 18,616.5 / 27,932.9 | 15 Sep research | Same basis, same agreement | Reproduced |
| v2 constants S 210.3398, M 855.6651, L 2,399.3371, XL 6,727.8875, R 17,713.3258 regenerate from the manifest | plan A3 | Re-implemented from scratch (÷ source PUE, x = out + 0.05·in, coefficient = max(0, Σxy/Σx²), XL = L²/M): agreement to < 1e-5 % | Reproduced |
| Ember World 2025 = 458.49, revised 2024 = 471.46, 91 countries with 2025 values | plan A7a, D1 | Live CSV downloaded; SHA-256 matches the pinned hash; importer regenerates `grid_ember_2025.json` byte-for-byte; own CSV scan gives the same three figures | Reproduced |
| Ember intensity is lifecycle all-GHG CO2e, 100-year | plan A7a | Ember methodology v1.5 PDF, verbatim: "full lifecycle emissions including upstream methane, supply chain and manufacturing emissions, and include all gases, converted into CO2 equivalent over a 100-year timescale" | Reproduced |
| 478 runs, 25 Aug–11 Sep; 433/41/2/2 by status; 442 estimates; 0 measured | validation-results | Own scan of `cloud-runs.jsonl`: identical | Reproduced |
| 414 replayable / 416 units / 64 excluded (36 + 28) | validation-results | Tool rerun byte-identical to stored JSON; own eligibility logic gives 414/416; 36/28 split recovered by hand (the tool reports one bucket) | Reproduced |
| 634.272 Wh (v1) vs 506.192 Wh (v2), −20.19%; per-class L −7.72, M −28.69, R −15.65, S −15.86 | validation-results | Own script with hard-coded constants: 634.27190525 / 506.1920355, −20.193% ; per-class identical | Reproduced |
| Cache reads 71.44% of input-side tokens; 91.84% on one day; 71.34% one final model | validation-results | 4,304,016 / 6,024,518 = 71.4417%; 439/478; 341/478 | Reproduced |
| Export content-free; 478 unique ids; SHA-256 pinned | validation-results | Row keys are usage/accounting only; ids unique; hash matches both manifests | Reproduced |
| 20 export pages | validation-results | Not verifiable from retained files (no page markers); consistent with `LIMIT 25` | Unverifiable |
| "28 focused tests passed" | validation-results | Files not named; replay + validation = 23, plus `test_class_ladder_v2.py` = 28 | Reproduced once the third file is guessed |
| Full suite 3,081 passed / 44 skipped; cloud 9 passed | plan Verification | Now 3,093 passed / 44 skipped / 0 failed (tree gained tests since); cloud 290 passed with `stripe` importable; ruff clean; `tsc --noEmit` clean | Reproduced and superseded |
| 87 new test functions in 13 new files | — | Counted | Confirmed |

**One interpretation error, not a numeric one.** The plan (lines ≈104 and
≈617–619) and the results file explain the −20.19% as "v2 normalizes source
PUE to IT coefficients". PUE alone moves each class by only 1/1.12 to 1/1.14
(about −11 to −12%). The v2 procedure also drops v1's hand-rounding and
switches from a two-parameter OLS to a single-coefficient fit with a fixed
0.05 input weight. Per class, in Wh/Mtok:

| Class | v1 shipped | v1 fitted b | ÷ source PUE | v2 shipped | PUE share of the v1→v2 change |
|---|---:|---:|---:|---:|---|
| S | 250 | 271.9 | 242.7 | 210.3 | roughly two thirds |
| M | 1,200 | 1,233.1 | 1,101.0 | 855.7 | about one third |
| L | 2,600 | 2,634.7 | 2,311.1 | 2,399.3 | more than all of it (fit method pushes L back up) |
| R | 21,000 | 20,850.4 | 18,616.5 | 17,713.3 | roughly two thirds |

M dominates the replay (48.9% of v1 Wh on 42 units), so most of the −20.19%
is fit-method and de-rounding, not boundary correction. The doc's qualitative
description of v2 is accurate; the causal sentence about the replay is not.

## 2. Findings, ranked

Severity: **fix before commit** / should-fix / note. Every item was confirmed
by reading code unless marked plausible.

### Fix before commit

**F1. The old mis-citation is still live on two user-facing surfaces.**
`backend/tret/services/emission_settings.py:262` returns the shipped default
as `458.49` with label "IEA global power-sector average"; the settings-API
tests assert the value only. `frontend/src/components/shared/emissions.ts:471`
renders "Ships as the IEA global power-sector average" for every
`global_default` run. Also stale: `backend/tret/services/emissions.py:10,
16–17, 22` (docstring says least-squares, fitted 20x ratio, IEA default) and
`docs/grid-zones.md:131`.

**F2. The −20.19% is mis-attributed** in `emissions-plan-2026-09-17.md`
(A12 row and the historical-execution section) and in
`validation-results.md`. Replace with the decomposition above and say that
class M's movement is mostly fit-method.

**F3. Component lists contradict the arithmetic.** `emissions.py:2787–2797`
emits `excluded_components: ["facility_overhead", …]` for the default v2
branch and for `node_it`/`gpu` measurements, while `emissions.py:2645–2648`
has already multiplied by PUE and `emissions_coverage.py:75–77` says overhead
is included. A consumer using the A2 lists as intended would add overhead
twice. Decide whether the lists describe the coefficient boundary or the
result, and say which in the record.

**F4. Two shipped defaults change on `git pull` with no upgrade note, and
one nulls dashboards.** Grid default 470 → 458.49 and `energy_strategy`
default → `class_ladder_v2` (`emissions.py:906`) change every new run's
figures; `docs/upgrading.md` has zero emissions content. Separately, the new
factor-identity rule (`api/analytics.py:617–622, 657–664` with
`grid_comparison_signature` at `emissions.py:3147–3169`) falls back to exact
factor identity whenever `includes_td_losses` is unknown, which is always for
the shipped default. Any window mixing pre- and post-upgrade runs, or any
workspace that corrects its own grid factor, now returns `co2e_g: null` on
totals, `by_model` and `by_harness` under one identical basis. Intentional
per the deleted comment, but undocumented and user-visible.

**F5. Multi-run legacy deliverables silently lose their footprint
paragraph.** `services/export.py:189–193` requires one known boundary; every
pre-batch row has `energy_boundary` unknown, so any deliverable backed by two
or more historical runs drops the whole "estimated compute footprint" block
(`export.py:121` guard) instead of printing "coverage unknown". Untested:
both export tests use a single run. Contradicts the code's own comment at
`export.py:123–125`.

**F6. Local metering self-disables under ordinary jitter, and the reason is
then overwritten.** `services/energy_collector.py:175–181` accepts a claim
only if meter coverage matches claim wall-clock within
`min(1 ms, interval/1000, duration/1000)`; the meter's own start/stop
timestamps sit on the other side of `task.cancel(); await task` and, for
NVML, after a `to_thread` hardware query at both ends. A 1.5 ms cancel
round-trip discards the measurement and the run falls back to the estimate.
Then `engine/harness.py:905–920` recognises only the literal status
`"incomplete"`, so every real collector status (`unallocated_concurrency`,
`incomplete_claim_interval`, `meter_failure`, …) is rewritten to
`meter_unavailable`. Fail-closed, so no wrong number, but the A6 diagnostics
are erased exactly when needed. Tie the tolerance to the sampling interval.

**F7. Accounting can now fail or slow a run.** (a) `harness.py:1123–1130`
runs `_combine_segments` (with an `assert`) and `db.commit()` unguarded
inside the `finally` that exists to clear `self._cancelled`; a commit error
after a failed run skips line 1140 and leaks the id. (b) `emission_calls.py:
87–90` re-resolves the full factor ladder once per call record on every
turn, and `build_factor_set` is deliberately un-memoised, so turn k costs k
resolutions (≈5,000 for a 100-turn run), each re-parsing the override
documents; this path also lacks the `try/except` that `_factors_for` has
(`harness.py:765–782`). Cache the per-call `FactorSet` by (served_by, hour)
and wrap the call.

### Should-fix

**F8. The v1 rollback keeps the double PUE and nothing in the record says
so.** Under `class_ladder_v1`, `emissions.py:2645` still applies deployment
PUE to facility-fitted constants; the record carries
`energy_boundary: unknown` and a legacy calibration id but no caveat, and
`_class_ladder_note` is the original prose. A rollback run should name the
known defect.

**F9. Ember attribution is claimed but not carried.** `grid_ember_2025.json`
has `license: "CC BY 4.0"` and nothing else; no `attribution`, no
`license_url`, no creator credit, while `docs/emissions-methodology.md:699–701`
says the asset has "CC BY 4.0 attribution". CC BY 4.0 §3(a)(1) requires the
creator be identified. The ODbL table already shows the shape
(`grid_zones.py:564–568` writes `license`, `license_url`, `attribution`). No
NOTICE or THIRD_PARTY file exists in the repo.

**F10. Per-call `served_by` / `inference_geo` are persisted without a length
or charset cap** (`anthropic.py:282–286`, `openai_compat.py:216–221`) into
JSON columns and the SDK ledger on every turn. A `[:64]` cap and a slug regex
close it.

**F11. The AWS PUE disclosure is probably unreachable.**
`emission_factors.py:1539–1542` maps only `google-vertex` → `google`; nothing
maps Bedrock's slug (`amazon-bedrock`) to `aws`, and the direct Anthropic
provider reports `anthropic`. Decision D3's AWS half does not ship in
practice. Harmless, but the plan says it does.

**F12. `inference_geo` is likely inert.** Both adapters read it off the
message body; the plan cites `usage.inference_geo`. Tests only exercise a
fake with a top-level attribute. If the live API nests it under `usage`,
every run records `None`. Check `usage` first, then the message. (Plausible;
cannot be verified offline.)

### Notes

- **Caveat direction deviates from A5.** `emissions.py:2916–2927` sets
  `direction: "either"` for all reasoning cases with the justification that
  the calibration denominator is unresolved; the plan's acceptance said
  `overstates` for counted-in-output providers. Never both, so the invariant
  holds; confirm the deviation is intended.
- **Evidence-based uncertainty has no production caller.**
  `Evidence.from_records` / `EvidenceRecord` are constructed only in one test;
  with `band.derived: true` the derived band now always equals the configured
  band. That is the intended fail-closed direction, but the A11 phrase
  describes an API seam, not a live path, and `_OPERATOR_LAYERS` plus its
  comment at `emissions.py:1470–1472` are dead.
- **The A8 allocator is unreachable and rejects partial coverage.**
  `embodied_allocation` has no caller outside `energy_accounting`, and
  `emissions.py:2658–2660` raises when `complete_total_g` is `None`, which is
  exactly what `allocate_by_time` returns with an unknown component. The
  double-add guards also raise inside the accounting path rather than degrade.
- **Cloud embodied silently activates for existing configs.**
  `_resolve_embodied` moved its non-local short-circuit below the operator
  rungs; a workspace that already had `embodied.g_per_run` set now adds grams
  to every cloud run with no opt-in. Intended direction, unannounced.
- **Unwrapped data load.** `_resolve_grid` calls `ember_entry_for_region`
  outside the `try/except` that guards the zone table; a missing
  `grid_ember_2025.json` raises inside accounting for any `country-XXX` pin.
  Related: the data files and eight new service modules are untracked and
  must be `git add`ed or production loses them.
- **XL is not labelled interpolated in a v2 run's record**; only
  `anchor_model: null` hints at it. A3 asked that XL "says so".
- **One inferred historical boundary**: `analytics.py:1256–1261` labels a
  legacy row `gpu` when `energy_meter.kind == "nvidia_smi"`. Evidence-based
  and arithmetically neutral; noted as the batch's one guess.
- **Masked null→0** at `analytics.py:533` for structured-partial rows, safe
  only because `carbon_available: False` blocks summation; and those runs are
  skipped from `by_basis`, so `by_basis` energy no longer reconciles to the
  window energy total.
- **Shape changes**: `run.model_timeline` is now written on every turn and a
  never-switching run has a one-entry timeline where it had `None`;
  `energy_accounting["models"]` repeats the same model once per call.
  `client.ts:1720–1722` still says per-basis rows are always summable.
- **Stale comments**: `uncertainty_derivation.py:63–64`; the plan's
  "unverified Microsoft figure excluded" is true of selection, but the value
  still appears in the shipped-default PUE label text
  (`emission_settings.py:275–278`).
- **What-if preserve mode** re-estimates *estimated* runs through today's v2
  ladder, so their delta mixes method change with factor change. Documented
  behaviour; worth a sentence in the response's `basis`.

## 3. Progress-table truth check

| Row | Verdict | Reason |
|---|---|---|
| A0 | partially | Tooling and reconciliation tests exist; the figures live outside the repo and reproduce from the private export, not from the tree |
| A1, A2, A3, A4, A6, A6a, A7b, A9/A10, A11a | supported | Module and named tests found for each claim |
| A5 / A5a | supported | All four places (providers, harness, SDK, local receipts) carry per-call records; `inference_geo` is inert in practice (F12) |
| A7 / A7a | partially | Data, lookup and default hold; "separate CC BY attribution" does not (F9) |
| A8 | partially | Allocator and guards exist and are tested, but unreachable through accounting for the unknown-component case |
| A11 | partially | Ledger and quality gate hold; "corrected deliverable export" has no new test and regresses multi-run legacy documents (F5); "evidence-based uncertainty" has no production caller |
| A12 | partially | Replay reproduces exactly; the causal explanation is wrong (F2) |

## 4. What holds up

- Exactly-one-PUE in arithmetic: the only three multiplications are the gated
  line, the v1 shadow, and the baseline's own; facility and partial
  measurements are never re-multiplied; a GPU reading is never relabelled.
- Baseline resolves through the same frozen `FactorResolutionContext` as the
  run, with tests for workspace override, cross-provider region + hourly at
  the same `at`, and run override on both sides.
- What-if identity reproduces the recorded object before any catalog lookup;
  facility + PUE change is an atomic 422; exclusions are symmetric.
- Replay reads only recorded factors, applies no PUE or grid, excludes
  measured/override/ambiguous records, reconciles segments and calls to the
  parent, asserts input immutability.
- Coverage envelope withholds the complete total permanently while
  idle/network/tool components are unknown; zeros require evidence.
- Metering: reset, device change, negative delta and non-finite readings
  handled; partial readings never replace the estimate; conservation tested.
- Task ledger: failed attempts in the numerator, duplicate and
  parent-inclusive rows rejected, zero accepted → undefined.
- SDK/CLI boundary plumbing validates NaN/inf/negative and restricts values.
- Candidate anchors carry boundary, denominator, statistic, separate code and
  data licences, `numeric_anchor: null` and
  `status: candidate_not_calibration_input`; no unverified magnitude.
- Docs are otherwise well hedged: the runbook and UI say method changes are
  not savings and conformity is not established.

## 5. Recommended order before commit

1. F1, F2, F9 and the docstring cleanups: label and text edits, an hour.
2. F3 (decide what the component lists describe) and F8 (rollback caveat).
3. F5 (treat a missing legacy boundary as the historical meaning in the
   export, or print "coverage unknown"; add a two-run test).
4. F6 and F7 (tolerance, status passthrough, guard the `finally`, cache the
   per-call factor set).
5. F4: write the upgrade note covering both default changes and the
   factor-identity nulling, and decide whether the identity rule should treat
   the shipped default's unknown T&D flag as equal to itself.
6. F10–F12 and the notes as time allows.
7. `git add` the untracked data and modules explicitly (this checkout has
   concurrent sessions; stage paths, not `-A`), rerun the suite, commit.

## 6. Provenance of this audit

- Replication scripts and outputs: session scratchpad `replicate-jegham/`,
  `replicate-ember/`, `replicate-replay/`.
- Suites rerun 2026-09-17: core 3,093 passed / 44 skipped / 0 failed in
  114 s; cloud 290 passed; ruff clean; `tsc --noEmit` clean.
- Reviews: accounting core, providers/harness/metering, docs/frontend/claims;
  read-only, findings verified by reading code unless marked plausible.

## 7. Fix status — 2026-09-17 (later the same day)

All findings above were addressed in the working tree, uncommitted. A second
read-only review over the fixes confirmed each at its location and raised
nine follow-ups, which were also closed. Final verification: backend suite
3,121 passed / 44 skipped / 0 failed; cloud 290 passed; ruff clean;
`tsc --noEmit` and `npm run build` clean; `git diff --check` clean; wheel
contains the Ember and calibration assets.

| Finding | Status | Where |
|---|---|---|
| F1 mis-citation | fixed | `emission_settings.py` shipped-default labels (Ember; PUE label Google 1.09 / AWS 1.14), `frontend/.../emissions.ts`, `emissions.py` docstring, `docs/grid-zones.md` |
| F2 −20.19% attribution | fixed | plan A12 row and historical-execution bullet, `validation-results.md` interpretation, decomposition tables in `emissions-methodology.md` and `eco-accounting.md` (PUE share −10.7% S/M/R, −12.3% L) |
| F3 component lists | fixed | `emissions.py` result-describing lists, `component_lists_describe: "result"`, `energy_boundary_of_coefficients`, `facility_overhead_via_pue` / `facility_overhead_measured`; `combine_accountings` included-wins; contract documented in the methodology doc |
| F4 upgrade note and identity rule | fixed | `docs/upgrading.md` dated section; `grid_comparison_signature` unknown-equals-unknown plus dataset_version and observation_year in the identity; specific `not_summable_note` for within-basis factor changes |
| F5 export regression | fixed | `export.py` legacy-boundary handling, paragraph never dropped, per-boundary subtotals; two new tests |
| F6 collector tolerance and status passthrough | fixed | `energy_collector.py` interval-tied tolerance capped at 10% (>1 s) or the claim duration (≤1 s); `harness.py` passes collector statuses through |
| F7 run safety and per-call cost | fixed | `harness.py` guarded finalize with nested finally so `_cancelled.discard` always runs, including on `CancelledError`; per-execution call-factor cache with fallback |
| F8 v1 rollback label | fixed | caveat `legacy_source_pue_double_count`, v1 class note |
| F9 Ember attribution | fixed | `grid_ember_2025.json` `attribution` / `license_url` / `creator`, importer emits the same, `THIRD_PARTY_DATA.md` at repo root |
| F10 slug cap | fixed | `providers/base.py::normalize_call_slug`, applied in both adapters (lower-case, ≤64, restricted charset) |
| F11 AWS slug | fixed | `UPSTREAM_PUE_ALIASES` with `amazon-bedrock` → `aws` |
| F12 inference_geo location | fixed | usage first, body second, both adapters |
| Notes: XL flag, allocator never raises, Ember load guarded, what-if method note (incl. unstamped legacy rows), N1 null→0, by_basis reconciliation, stale comments | fixed | see the follow-up list in the review |
| Tests adjusted to new behaviour | done | `test_emission_factors.py` double-count → caveat; `test_eco_accounting.py` two legacy runs sum; `test_openrouter_request_hygiene.py` slug case |

Left as recorded decisions, not changed: reasoning caveat direction stays
`either` (deviation from the plan's `overstates`, noted in the plan); the A8
allocator still has no production caller; `model_timeline` is written on
every turn; `energy_accounting["models"]` repeats a model once per call.

**Before committing:** the new modules, data assets and tests are untracked.
Stage them explicitly (this checkout has concurrent sessions; do not use
`git add -A`). Cloud repo changes: `tret_cloud/emissions/managed_factors.json`,
`README.md`, `tests/test_managed_factors.py`.
