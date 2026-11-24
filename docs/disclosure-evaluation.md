# Disclosure evaluation engine

Experimental, off by default. Stage 1 (`docs/disclosure-collection.md`, "Stage 1 normaliser")
turns each captured report into one Markdown file. Stage 2, described here, finds the passages
of that file that cover a fixed set of **modules**.

Switch: `TRET_DISCLOSURE_EXTRACT=on`. While off, the `disclosure_extract` batch profile refuses to
create or launch batches (409 with the reason) and the `/api/report-extractions` routes are a 404.

## Modules

A module set is a YAML file (`version` must be a quoted string, `"0.1"`; an unquoted `1.10` would be read as the number 1.1 and is refused): `module_set` (slug), `version`, and per module a `slug`, `title`,
`definition` (what counts), `include` and `exclude` bullet lists and `max_words` (default 4000).
`tret/evaluation/modules.py` validates it; a bad file is refused with the reason.

The built-in default (`tret/evaluation/data/modules-v0.1.yaml`, `kith-climate@0.1`) has four modules:

| slug | what it covers |
|---|---|
| `physical_climate_risk` | hazard identification, asset or site specificity, financial impact, physical-risk scenario analysis, adaptation |
| `targets_and_transition` | emissions targets (scope, base year, near and long term, SBTi status), interim milestones, transition plan, decarbonisation levers |
| `governance_and_capex` | board and committee oversight, management responsibility, incentive links, actual capital allocation (CapEx, EU Taxonomy-aligned CapEx and OpEx) |
| `sensitive_framing` | missed or withdrawn targets, restatements of baselines or prior years, boundary exclusions or changes, and how the report explains them |

`TRET_DISCLOSURE_MODULE_SET` names a file to use instead. A private pack can supply a tuned set that
way. The set (not the path) is copied into the batch's settings at creation, and every extraction
records `module_set@version`, so a changed set sits beside earlier results.

## Block ids and verbatim cutting

`tret/evaluation/blocks.py` splits a normalised report deterministically: each `## Page N` (or ESEF
`## Section N`) unit into blocks. A paragraph is a blank-line separated run of lines, a whole
Markdown table is one block, a heading is its own block. A block id is `p{page}.b{n}`
(`s{n}.b{k}` in a section). The model is shown the text with every block prefixed `[p12.b3]` and
answers with ids only. `cut(start, end)` returns the original text of the blocks in between, across
pages, without ids or page markers; blocks are joined with one blank line. Nothing the model
writes ends up in a module. Every id is checked against the segmentation; unknown or reversed spans
are dropped and listed in the extraction's `issues`.

## Windowing

A report larger than one call is cut into page-aligned windows (a page bigger than the window is split at block boundaries; a single block bigger than the window fails that window with an issue). A span or evidence block must lie inside the window the model was shown. Page markers count only when sequential (`## Page N` after page N-1), as in `services/page_check.split_pages` of about `TRET_EXTRACT_WINDOW_TOKENS`
(default 120000, estimated as chars / 4), one model call each. Windows merge deterministically:
frameworks are the union; assurance comes from the window that cites a valid block (the strongest
level wins); the NACE proposal with the highest confidence wins; module spans are concatenated and
a module is present when any window says so and a valid span survives. Overlapping or adjacent
spans are joined. A module above its `max_words` keeps its highest-confidence spans and is flagged
`truncated`. `present=false` is a valid answer. If some windows fail the extraction is `partial`; if
all fail, `failed`.

## Model choice: routing, never pins

Every call goes through the model router (`tret/engine/routed_call.py`) with the batch's **model
policy**, asking for a context window of at least input plus output tokens. A policy that pins a model
is refused. The policy is an auto policy such as `objective=eco,open_weights=prefer`, optionally
`allowed` and `max_cost_tier`, given with `--policy` (JSON, or `key=value` pairs, `allowed` separated by `|`).
The run records the routing decision and its rationale (`Run.routing`, with one entry per call under
`structured_calls`). One retry on a transient provider error or an invalid JSON answer; a context
overflow or a provider refusal fails clearly.

## How it runs: direct batch targets

A batch profile with `direct = True` still gets a `Run` row per target (task type `structured_call`) under the system-managed **Disclosure Extractor** harness (no tools, created on first use per workspace; never the Disclosure Collector),
so cost caps, workspace budgets and credit gates, pause-on-credit, post-run hooks (billing) and the
runs UI work as for any run. `Run.cost_usd` is the catalog price, `Run.reported_cost_usd` the
provider-reported cost when there is one (else the catalog price), and energy and carbon come from
the same accounting the agent harness uses. These runs are excluded from `run_outcomes`, so they are
not evidence for the router until extraction quality is scored.

## Running it

```
tret disclosure normalise --batch <collection batch id>
tret disclosure extract --from-batch <collection batch id> --dry-run --policy objective=eco
tret disclosure extract --from-batch <collection batch id> --policy objective=eco
```

`--dry-run` prints the number of reports, windows, estimated input tokens and a cost range from the
catalog (cheapest to dearest model; the router picks the real one) and calls nothing. Without it a
**draft** batch is created; start it with `POST /api/collections/<id>/start` (or in the UI) so the
running server executes it. `--only-missing` skips reports already extracted at this extractor
version and module set; `--limit N` takes the first N reports.

When a report's company has no `sec_sic` or `manual` classification, the extractor's NACE proposal is
stored as an `extractor` row in `company_classifications` (key: cik, lei or ticker, else the target
slug) with its confidence and evidence `{document_id, page, block}`.

## Tables and API

`report_extractions` (one per report, extractor version and module set: metadata, windows, issues,
status) and `report_modules` (one per module: present, spans with pages, text key and sha256, word
count, confidence, truncated). The text is in the blob store under `extractions/<report>/<version>/<module set>/<module>.md`, so two module sets never share a blob. An ESEF section's number is in `start_section`/`end_section`, never in `start_page`.

A result never replaces a better stored one: a `failed` extraction never overwrites an `extracted` or `partial` one, a `partial` never overwrites an `extracted` one. The attempt is recorded in the kept extraction's `issues` (`later_attempt_not_stored`). `models` lists every model that answered.

* `GET /api/disclosure-reports/{id}/extractions`
* `GET /api/report-extractions/{id}`: with its modules
* `GET /api/report-extractions/{id}/modules/{module}/text`: the verbatim text
* `GET /api/report-extractions/review`, `PUT /api/report-extractions/{id}/review`: the review list (below)

`report_modules` also carries the claim density (`claim_count`, `claim_density`, `claim_counts`,
`density_version`, null for an absent module) and `extraction_reviews` the review decisions.

## Claim density

A word count rewards length: a module that says little in many words scores like one that commits to
numbers, dates and named places. `tret/evaluation/density.py::claim_density(text)` counts the
checkable specifics in a module's text, as a counter-signal to `word_count` for length bias when
Engine 1 compares reports. It is a regex counter with no model call, so the same text always gives the
same numbers. `claim_density` is claims per 100 words, over the same word count as
`report_modules.word_count`; `claim_counts` is the breakdown.

A token is counted once, in this order (a matched span is masked before the next category):

1. **currency**: an amount with a symbol (`€ $ £ ¥`, `US$`) or ISO code before or after it, with an
   optional scale word: `€12.5 million`, `USD 2.3bn`, `50 million euros`.
2. **date**: full dates, day and month, month and year, `Q3 2024`, `H1 2025`, `FY2024`/`FY24`, and
   stand-alone years 1900 to 2100 (a year followed by a unit, `2025 tCO2e`, is a quantity).
3. **unit**: a number or range with `%`, percent, basis points, `tCO2e` and variants, tonnes, `kWh`
   to `TWh`, `kW` to `GW`, `GJ`, `m³`, litres, `ha`, `km`, `°C`, `ppm`, with an optional scale word.
4. **number**: any number left over (`14 suppliers`; `5-10` is one).
5. **site**: one to four capitalised words before a site noun (`Rotterdam refinery`, `Port Hedland
   terminal`), `Gigafactory Berlin`, `Port of Rotterdam`. Leading generic words (`The`, `Our`,
   `Each`...) are dropped, so `Our refinery` is not a site.

So `42% by 2030` is one unit and one date, and `1,234,567 tCO2e` is one unit. Ignored first: page and
section markers, heading numbering (`1.2.3 Title`, `## 4.1 Targets`; a bare `2.5 Million tonnes` is a quantity), list markers (`1.`, `(a)`), footnote markers
(`[12]`) and superscripts, URLs, and cross-references whose number names a place in a document
(`Scope 3`, `Table 4`, `Article 8(1)`, `ISO 14001`, `ESRS E1`, `p. 12`; a year or a `3,400`-style number after one is the next claim, not part of the reference). Known limits: it counts
tokens, not distinct facts (a figure repeated in a table counts every time), it does not read
spelled-out numbers, and a sentence-initial capitalised word before a site noun counts as a site name.

`DENSITY_VERSION` (`claims-1`) changes with any rule. The extractor fills the columns when it stores a
module; `tret disclosure claim-density --from-batch <id> [--force]` fills or refreshes them for
modules at another version (or all, with `--force`). A module whose text is missing from the blob
store is skipped and counted.

## Extraction review list

`tret disclosure review-list --from-batch <id> [--kind K] [--all]` and
`GET /api/report-extractions/review?batch_id=&kind=&include_resolved=` list what a human should look
at, most severe first. The list is computed from the stored extractions on every call (a rule or
threshold change applies at once); only the reviewer's decision is stored.

| kind | when |
|---|---|
| `extraction_failed`, `extraction_partial` | the extraction's status (a failed one lists only this) |
| `nace_low_confidence` | the company's effective classification is the extractor's own, below the confidence floor |
| `nace_missing` | the company has no classification at all |
| `gics_disagrees` | the effective classification is not manual and the register's GICS sector does not fit it |
| `module_low_confidence` | present module below the confidence floor |
| `module_present_no_text` | present but no text or zero words |
| `module_truncated` | cut to the module's `max_words` |
| `module_low_density` | 100+ words and fewer claims per 100 words than the floor: likely boilerplate |
| `span_issues` | one per (issue kind, module) of `invalid_span`, `reversed_span`, `present_without_valid_span`, `invalid_block` |

Floors: `TRET_DISCLOSURE_REVIEW_MIN_CONFIDENCE` (0.6) and `TRET_DISCLOSURE_REVIEW_MIN_DENSITY` (0.5).
Each item has a stable `item_key` (`status`, `module:<slug>:low_confidence`, `nace:low_confidence`,
`issue:invalid_span:<slug>`...). `PUT /api/report-extractions/{id}/review` with
`{item_key, decision: accepted | dismissed | fixed, note}` (approver role) records a decision, and a
decided item is hidden unless `include_resolved` / `--all`. A NACE problem is fixed with
`PUT /api/company-classifications/manual`, then marked `fixed`. The three NACE kinds are about the company, so each is listed once per company, on its most recent non-failed extraction. Status items have the keys `status:failed` and `status:partial`. Re-extracting a report (a result that replaces the stored one) deletes that extraction's decisions, so the new result gets a fresh review. The GET takes `limit` (default 500, max 5000) and `offset` and returns `total`. `batch_id` is the source collection
batch or the extraction batch.

## The 30-report hand check

1. Pick 30 reports across document types and sizes (include several multi-window ones).
2. For each, open `/api/disclosure-reports/{id}/markdown` beside each module's text endpoint.
3. Per module, check: **present** is right (the report really covers it, or really does not); the text
   starts and ends at the right passage; nothing important in the report was left out; nothing
   unrelated was included; `truncated` modules lost nothing essential.
4. Check the metadata: type, year, frameworks, assurance block, NACE division.
5. Read `issues`: many `invalid_span` entries mean the model is losing track of ids (try another policy).
6. Record the verdicts per module; false negatives (a covered module marked absent) matter most.
