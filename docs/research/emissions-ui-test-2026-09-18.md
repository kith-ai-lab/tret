# Tret Cloud UI test pass after the emissions batch

**Date:** 2026-09-18 (UTC), against cloud release 46 (core `1075f13`, cloud
`ade07ff`). **Driver:** the Browser pane, signed in as the analyst account in
its personal workspace; API reads through the page's own session for
verification. **Spend:** three chat runs on the pinned cheapest model
(gemini-3.5-flash-lite, class S), about $0.015. **Basis:** the agentic test
protocol's smoke set plus the emissions surfaces this batch changed.

## Results

| Probe | Result | Evidence |
|---|---|---|
| Settings → emissions factors labels | pass | Grid default reads "458.49 gCO₂e/kWh — Ember Yearly Electricity Data, World 2025 CO2 intensity (lifecycle, all GHG, 100-year CO2e), CC BY 4.0"; PUE label "Google 1.09, AWS 1.14"; effective factors 458.49 on every provider; no "IEA" anywhere |
| Emissions view renders and separates bases | pass | Window of 477 runs mixes location- and market-based; window carbon withheld with the specific note; energy 811 Wh and money still totalled; by-model rows keep carbon where one factor applies |
| CHAT-01 one turn, pinned model | pass with note | Completed on flash-lite, `routing.override == run_override`, names all four datasets, cost 0.00187, CO₂e 0.0534 g. Note: no tool call was made (grounding `skipped`); the row counts came from the capability block in context, so the protocol's "activity contains lookup_dataset" line does not hold for this prompt |
| CLIM-01 hand re-derivation, new method | pass | 0.05×4093 + 257 = 461.65 weighted tokens × 210.3398 Wh/Mtok = 0.097103 Wh; ×1.2 = 0.11652 Wh; ×458.49/1000 = 0.053425 g; matches the record to six decimals |
| CLIM-01 on a cache-heavy multi-call turn | pass | 1,334 in / 1,486 out / 17,052 cache read / 40 cache write → 1,639.96 weighted → 0.3450 Wh → 0.4139 Wh → 0.1898 g; four per-call records sum to the run total |
| Run record carries the new contract | pass | `method_id class_ladder_v2`, `energy_boundary node_it`, `component_lists_describe result`, `facility_overhead_via_pue` included, coverage envelope with `complete_total null` and four missing components, `energy_method_shadow` v1 at 0.0635 g labelled `methodology_correction_not_emissions_savings`, grid record with Ember dataset version and 2025 observation year, caveat `reasoning_counted_in_output` |
| Run detail page rendering | pass | "CO₂E covered subtotal (est.)", "Coverage: node it", "Accounting coverage · one run", legacy-estimate sentence, 458.49 global default, scope split, step-by-step derivation |
| BILL-01 ledger debit | pass | Usage row −0.002432 for reported cost 0.00187 (× 1.3 = 0.002431); balance chain intact |
| CHAT-02 two turns, subject carried | pass | Turn 1 named the 2018 vintage and the rising signal, grounding clean, delegated a divergence assessment; turn 2 ("And what about Willow Bend for flood?") resolved to S-006, score 71, vintage 2024, "agree"; `_history` had exactly two entries with no tool results; two distinct runs, both on the pinned model |
| CHAT-09 empty and whitespace send | pass | Send button disabled for empty and whitespace-only input |
| What-if identity scenario | pass | 1-day window, 5 runs: recorded 3.46758 g, scenario 3.467579 g, delta 0% |
| What-if doubled grid factor | pass | 916.98 g/kWh scenario: 6.935157 g, delta exactly +100%, 0 runs skipped |
| "Compare a scenario" panel | pass | Opens with energy-calculation mode selector, grid default, region pin, PUE and band fields |

## Defects found

| # | Severity | What | Where | Status |
|---|---|---|---|---|
| U1 | S2 | Identical grid factors split into two "signatures" because runs recorded before 2026-09-04 lack the `grid_co2e_layer` and `grid_temporal` keys. The location-based row (441 runs, all 470 g/kWh, PUE 1.2, global default) withholds carbon and the note says "2 different grid-factor signatures within the same GHG basis (470.0 gCO2e/kWh)" | `emissions.py::grid_comparison_signature` includes source/label/layer in the identity | fixed in core c7f051e; verified on release 47: location-based row now splits only on the genuine 458.49 vs 470 change, terra/luna rows regained carbon |
| U2 | S2 | Same root cause in the deliverable footprint: "5.2 Wh · — at 470 g/kWh, over 3 runs" for three legacy runs on one factor | `export.py::_deliverable_footprint` compatibility check | fixed in c7f051e; verified: "5.2 Wh · 2.9 g CO₂e at 470 g/kWh, over 3 runs" |
| U3 | S3 | OpenRouter reports the upstream as `google-vertex/eu`; the alias map keys on `google-vertex`, so the managed Google PUE (1.09) never selects and runs stay at 1.2 global default. Per-call `served_by` is recorded correctly | `emission_factors.py` alias lookup | fixed in c7f051e; verified with run 07bdf52f on release 47: PUE 1.09, layer managed, Google disclosure attached, upstream google-vertex/eu |
| U4 | S3 | Settings "Footprint" panel shows "runs with an estimate 0 / without 0" beside 761 Wh, and prints a garbled sentence ("Recorded under the Estimated from stored per-run accounting … basis. No basis was recorded on these runs") | cloud footprint card (tret-cloud 26a9e07) | fixed; verified: 419 runs with an estimate, 31 without, no garbled sentence |
| U5 | S4 | Per-run PUE factor note still cites "AWS 1.15, Microsoft 1.16" | `emissions.py` PUE reference note | fixed in c7f051e |
| U6 | note | CHAT-01's oracle expects a `lookup_dataset` activity; with the capability block in context the model answers without a tool call and grounding reports `skipped`. Protocol wording, not a product defect | protocol | record |

## Fix deployment

Core `c7f051e` and cloud `26a9e07` pushed; kith-tret-cloud release 47 and kith-bench redeployed 2026-09-18; health 200 on both. Spend for the pass including the verification run: about $0.02.

## Not run

Deep and adversarial levels, SYNC and TWOWAY (need the team workspace and
SharePoint residue policy), BILL beyond the ledger row, and anything needing
the qa- accounts.
