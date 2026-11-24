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

## Jury selection and per-pass freeze

Engine 1 judges pairs of report modules with a jury (one juror per match, rotated across model
families, plus a 10% double-judged sample); Engine 2 makes one sealed call. Both must run on models
chosen by the router, shown before any spend, and held fixed for the whole measurement pass.
Switch: `TRET_EVAL_JURY=on` (the HTTP routes are a 404 while off; the CLI preview is not gated).

**Why this is not a pin.** A model is never named: the policy (objective, `allowed`, cost ceiling,
`open_weights`) constrains, a family is a label (`openai`, `google`, `meta`, ...; `router_llm/families.py`),
and the router picks. Within one pass the pick is made once, recorded and frozen, so the instrument
does not change under the measurement; exploration is off inside a pass. The next pass asks the
router again and may pick differently. That is selection per pass.

**How jurors are ranked.** `ModelRouter.route_jury` takes the models the policy permits (the same
filters as normal routing: provider keys, `allowed`, cost ceiling and floor, open-weight preference,
ZDR, context window; a model that cannot hold the prompt is never a best-effort juror), orders them
with the router's evidence-led ordering (`candidate_sort_key` over the pooled priors, ties by model
id), drops models in a provider-error cooldown, and takes the best-ranked model of each distinct
family until k. It makes no model call of any kind, not even the router's own LLM step. Fewer than k
distinct families is an error that says how many there were and why the rest were excluded; it never
returns a short jury. `distinct_authors=false` allows several models of one family.

**What is recorded** (the `JurySelection`, stored whole in `routing_freezes.selection`): per pick the
model, family, rank, the evidence used (tier, runs, quality) and a plain-English `why`, plus the
next-best models of the same family; the families that were eligible but not seated; the models
excluded and why; the policy, k, objective, ranking version (`jury-v1`), router version, priors
version and timestamp. `routed_json_call(..., freeze=selection, juror_index=i)` then calls the frozen
model with no new routing decision (the decision is marked `frozen` with the freeze id); a retry stays
on the same model, and if that model is gone the call fails rather than falling back. Runs made by a
juror or the sealed call carry the `_eval_juror` / `_eval_sealed_call` task-input marker and never
become `run_outcomes` rows or router evidence.

**Policy still applies to a frozen call.** The frozen model skips routing, not the policy: the call's
own policy (cost ceiling and floor, `allowed`, open weights, ZDR, provider key) is checked against it,
and a model outside it fails the call with `frozen_outside_policy`; nothing else is tried. A
frozen model that has left the catalog or lost its key fails with `unroutable`, likewise without a
fallback. Local models are seated on a jury only when their context window is known and holds the
requested `min_context_window` (normal routing exempts local models from that filter). Without a
prompt size, ranking reads the priors of the size band of `min_context_window`.

**Families** are the publisher's lab (`router_llm/families.py`), so a model derived from another
lab's (nemotron on Llama, an R1 distill of Llama) counts as the publisher's family. A family
key for `task_family` priors is scoped to the caller's workspace on the server; the HTTP API takes none.

**Previewing.** No model is called and nothing is stored:

```
tret eval jury-preview --k 3 --policy-json '{"objective":"balanced","open_weights":"prefer"}' --min-context 60000
POST /api/eval/jury/preview   {"model_policy": {...}, "k": 3, "min_context_window": 60000, "purpose": "eval_jury"}
```

Freeze with `POST /api/eval/jury/freezes {"pass_ref": "...", "purpose": "eval_jury" | "sealed_call", ...}`
(workspace approver). Repeating a `pass_ref` (or a concurrent request for it) returns the stored selection and never re-picks; a
`k=1` freeze is Engine 2's one model per pass. `GET /api/eval/jury/freezes/{id}` reads one and
`POST .../release` ends the pass (the record stays; a released `pass_ref` is not reused).

Note: on a self-hosted install with `TRET_LOCAL_PROBE_TOOLS` on and a local model server configured,
the first preview in a fresh process warms the model catalog, which may probe local models for tool
support (a call to your own server, no cost). The probe lives inside catalog discovery and is not
skipped for previews.

## Tournament (Engine 1)

Engine 1 rates how well each company discloses one module (for example physical climate risk)
against its industry peers, by pairwise judgement rather than a score on a scale. Off by default:
`TRET_EVAL_TOURNAMENT=on` (and `TRET_EVAL_JURY=on`, because the jury is seated through the freeze
above). `TRET_EVAL_TOURNAMENT_CONCURRENCY` (default 2, at most 4) is how many juror calls run at
once; they wait on the network, so the small machine copes.

**Pools.** `peer_pool_snapshots` records the corpus a tournament was built from: the extracted
modules of one module slug (`present`, with text, from an `extracted` or `partial` extraction; the
newest usable extraction per report), each company's effective NACE classification (manual, then
SEC SIC, then extractor; unclassified modules are left out and counted). One module is kept per
company and fiscal year (the best, newest extraction; the company key is the register's CIK, LEI,
ticker or target) and none with identical text; what was dropped, and why, is in the snapshot's
`corpus_filter.dropped`. A pool starts at the
division. A division with fewer than `min_n` (15) modules goes up to its cluster, and a cluster
still under `min_n` goes up to its section; a section under `min_n` stays and is flagged `small`
(rated if it has at least two). A pool is split by regime (`csrd` from ESRS/CSRD in the frameworks
cited, else `uk` or `us` from the country, else `other`) only if every part has `min_n`. The same
input gives the same snapshot, and a snapshot is never edited: a rerun builds a new one.

**History is immutable.** A pool member (`peer_pool_members`) is what a tournament judges: it copies
the module's text location, sha256, word count and claim density, the strings to mask and the
company label at snapshot time. Matches and ratings point at members, and the links back to the
report rows are `ON DELETE SET NULL`, so re-extracting a report or deleting a report module never
changes or removes a tournament. Module text is content-addressed in the blob store
(`extractions/<report>/<version>/<set>/<module>-<sha256 prefix>.md`), so a re-extraction writes a new
blob and leaves the old one. A juror reads the member's blob and verifies its hash; a changed or
missing blob fails that match, with the reason, and it does not count. (Old blobs are not garbage
collected.)

**Show before spend.** `tret eval tournament create --workspace ID --module SLUG` (or
`POST /api/eval/tournaments`) writes a `draft` and prints the preview: pools, N, rounds
(`min(8, ceil(log2 n) + 2)`), the estimated matches (primary, plus 5% swaps and 10% doubles) and a
cost range, and the jury the router would seat with the reason for each pick. A preview nobody started
is deleted, with its snapshot, by `DELETE /api/eval/tournaments/{id}` or `tret eval tournament delete`.
It calls no model and
spends nothing. The cost estimate is words x 1.5 tokens for the two texts, plus 700 prompt tokens
and 250 output tokens per match, priced at each previewed juror's catalog price (the jury rotates,
so the range runs from the cheapest juror for every match to the dearest). It is an estimate, not
a quote.

**Start.** `tret eval tournament start ID --budget USD` (or `POST .../start {"budget_usd": ...}`,
approver role) freezes the jury once (`eval_jury`, pass `tournament:<id>`, three models from distinct
families, exploration off) and plays. Nothing is pinned; the policy given at create time constrains
the router. Before each match the runner checks spend plus what in-flight matches are expected to
cost against the budget (the larger of the catalog estimate and what matches have actually cost);
when the next match would not fit, the tournament pauses with `pause_reason: budget`. `resume`
continues, optionally with a larger budget. `cancel` stops scheduling (matches already in flight
finish). The CLI plays in its own process until the tournament completes, pauses or is interrupted
(Ctrl-C pauses it); the API runs it as a background task.

**Rounds and matches.** Each round pairs each pool Swiss-style by wins (+0.5 per tie), avoiding
rematches, with a seeded random generator (the tournament `seed` and the round), so a resumed
tournament never re-pairs a round (scores count the primary and double matches, never swaps). Each
pair is one primary match; the jurors rotate over the three families in a fixed order that depends
only on the plan. After the primaries of a round, 5% are re-judged by the same juror in the opposite shown
order (`swap`, the flip-rate sample) and 10% by a juror of another family (`double`). A juror sees
the module's definition and two texts in a random, recorded order (`shown_a_first`), with the
company name, short name, ticker, aliases, the classification's company name and the target's known
domains masked as "the company" (the number of masked strings per text is kept on the match as
`mask_count_a` / `mask_count_b`, to audit the masking); never the company, a rating or Engine 2
output. The text delimiters cannot be forged from inside a text: `<<<` and `>>>` are replaced by
look-alike characters in both texts. A failed match does not count.

**Pauses, not failures.** A transient error while playing a match (the database, the blob store) puts
the match back to `pending`, books what its run spent, and pauses the tournament with
`pause_reason` `error: <message>`. A match whose snapshot text can never be used (missing or changed
blob) is failed for good with `text_error:`; it does not count toward any failure rate, is never
replayed, and its pair counts as played. A refusal by the pre-run gate (credits or a limit) pauses the tournament
with `pause_reason` `credits`, and a frozen model that cannot be reached pauses it with
`juror_unavailable`; the match goes back to `pending` and is replayed on resume. If more than 20% of the
last 10 or more matches failed, it pauses with `juror_failures`; a round with more than 20% failures
pauses with `match_failures`. Resuming replays the failed matches of the unfinished rounds. Only an
internal error sets `failed`, and a failed tournament can be resumed too (its jury freeze is
released on failure; the selection stays on record and is reused). Every pause can be resumed. The
module definition (title, definition, include and exclude lists) is stored on the tournament at
create time, so a change to the configured module set cannot alter a pass in progress.

**Running safely.** Start and resume are conditional updates (a second one is a 409). A runner holds
a lease (`lease_owner`, `lease_until`, renewed every 30 seconds, valid two minutes); each match is
claimed (`pending` to `running`) before it is played, so two processes never play the same match.
The claim, the result write and the completion are each one statement that also requires the
caller to hold the running tournament's lease, so a runner that lost the lease cannot claim, record
a result or finish. What a run cost is booked to the tournament's spend once per run id
(`eval_match_runs`), whoever settles or recovers it, so a takeover never double-counts or loses a
paid call. A runner replaced in the same process is cancelled; an interrupted one (Ctrl-C,
shutdown) pauses the tournament with `interrupted`. A match whose run is still running (younger than
15 minutes) is not replayed: the new runner waits for it. A
`running` tournament whose lease has expired (`stalled` in the API) can be resumed. Juror calls share
one process-wide limit (`TRET_EVAL_TOURNAMENT_CONCURRENCY`), no database session is held open during
a call, and the text cache is bounded. A match left claimed by a dead runner is replayed; what its
run spent is added to the tournament's spend first. The freeze is released when the tournament
completes or is cancelled.

**Runs.** Every match is a `Run` on the system "Disclosure Jury" harness, executed through
`execute_direct` like an extraction target (pre-run gates, per-run cost cap, credits, energy), calling
the frozen model with no fallback. The run is stamped as a juror run, so it never becomes router
evidence or a `run_outcomes` row.

**Result.** After the last round a Bradley-Terry model is fitted per pool over the primary and
double matches (swaps are used only for the flip rate), giving each module a rating on an Elo scale
(pool mean 1500), a 90% interval and a decile or quintile band, written once to `module_ratings`
with the module's word count and claim density. The tournament `summary` holds per-pool counts, the
flip rate per juror family, the correlation (Pearson and Spearman) of rating with word count and
with claim density per pool and overall, and the jury. A length correlation above 0.6 or a flip
rate above 0.15 is flagged in `summary.flags` (the run is not stopped). A rating belongs to one
tournament and one snapshot; a rerun is a new tournament and never rewrites an old one.

**Restarts.** All state is in the database. At boot a tournament left `running` whose lease has
expired is marked `paused` (`restart`), never resumed on its own; one whose lease is still live is
left alone (it may be running in another process) and becomes resumable when the lease ends.
Ctrl-C of a CLI run pauses only that tournament.

**Refit.** `POST .../{id}/refit` or `tret eval tournament refit ID` recomputes the ratings and summary
of a completed or failed tournament from its stored matches, with no model call and no spend. The
fits run in a worker thread, outside any open transaction.

API (all 404 while off; creating and starting also need `TRET_EVAL_JURY`): `POST
/api/eval/tournaments`, `GET /api/eval/tournaments[/{id}]`, `POST .../{id}/start|cancel|resume`
(approver), `GET .../{id}/matches` (staff view, with each juror's model and rationale) and
`GET .../{id}/ratings`; also `POST .../{id}/refit` and `DELETE .../{id}` (a draft only), both approver.

## The 30-report hand check

1. Pick 30 reports across document types and sizes (include several multi-window ones).
2. For each, open `/api/disclosure-reports/{id}/markdown` beside each module's text endpoint.
3. Per module, check: **present** is right (the report really covers it, or really does not); the text
   starts and ends at the right passage; nothing important in the report was left out; nothing
   unrelated was included; `truncated` modules lost nothing essential.
4. Check the metadata: type, year, frameworks, assurance block, NACE division.
5. Read `issues`: many `invalid_span` entries mean the model is losing track of ids (try another policy).
6. Record the verdicts per module; false negatives (a covered module marked absent) matter most.
