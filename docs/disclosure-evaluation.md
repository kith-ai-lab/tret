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

## The 30-report hand check

1. Pick 30 reports across document types and sizes (include several multi-window ones).
2. For each, open `/api/disclosure-reports/{id}/markdown` beside each module's text endpoint.
3. Per module, check: **present** is right (the report really covers it, or really does not); the text
   starts and ends at the right passage; nothing important in the report was left out; nothing
   unrelated was included; `truncated` modules lost nothing essential.
4. Check the metadata: type, year, frameworks, assurance block, NACE division.
5. Read `issues`: many `invalid_span` entries mean the model is losing track of ids (try another policy).
6. Record the verdicts per module; false negatives (a covered module marked absent) matter most.
