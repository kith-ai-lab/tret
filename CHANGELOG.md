# Changelog

All notable changes to tret are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor releases may
include breaking changes; [docs/upgrading.md](docs/upgrading.md) covers every
one that needs action on an existing install.

## [Unreleased]

### Added

- **Weighted arms for split experiments.** Each arm of a split experiment can carry a
  `weight`; rows are dealt in proportion (smooth weighted round-robin within market and
  category, deterministic), so one arm can be a small challenger share. Every arm or none
  must have a weight, and every arm must get at least one row. The result reports each
  arm's `weight` and `share`, the experiment form has a Share field per arm and a button
  that fills an arm's model with the router's next untried candidate, and `start_batch`
  accepts weights. Replicate experiments are unchanged.
- **Exploration runs are marked on batch pages.** A target's JSON carries `explored`, the
  targets table shows an "exploration" chip, and the pending-reviews panel says that a
  success on such a run counts toward the model's record only after a review approval,
  not after a packaging-only approval or a judge score.

- Exploration share for agents on the scored ranking. A share of such an agent's runs equal to
  its policy's `adaptive.exploration` (default 0.05; 0 turns it off) goes to the most promising
  model it has not tried, within `adaptive.exploration_max_cost_tier`, and the run is marked in
  `runs.routing.exploration`. An exploration failure counts at once. An exploration success
  that has findings does not count toward the model's record or its cautious rate until one of
  them is approved in review, so unreviewed trial runs cannot promote a model. A batch run with
  a review rejection and no review approval now counts as a failure in the per-agent record.
  No migration.
- Scored ranking, live for agents that opt in. With `TRET_ROUTER_AGENT_SCORING=live`, an agent
  whose policy sets `adaptive.ranking: scored` is routed by the per-agent ranking: the ranked
  pick runs with no router call, and the decision stores its numbers in `runs.routing.scored`.
  With no record on the agent, an unscored objective (`quality`, `token_conservation`), learning
  off, or nothing fitting the context window, today's router decides. Every other agent is as
  under `shadow`, and the exploration share is recorded but not acted on yet.
  `token_conservation` no longer gets a shadow score, because recorded token counts leave out
  cache reads. No migration.
- Per-agent routing record, shadow only. With `TRET_ROUTER_AGENT_SCORING=shadow` each routing
  decision also stores, in `runs.routing.shadow`, what a ranking built on the agent's own
  results would have picked: expected cost, Wh or tokens per acceptable result on a cautious
  acceptable rate, a reliability floor measured against the best tried model, and a 10%
  exploration share for models the agent has not run. The routing preview returns the same
  block. It never changes the model that runs, and the default `off` is identical to before.
  No migration. See docs/architecture.md, "Per-agent routing record (shadow)".
- Router learning per batch family, behind a human-agreement gate: run outcomes gain
  `task_family` (derived from the batch, per workspace; never stamped) and an
  evaluator-blended `evaluated_quality_score`. Once 15 spot checks agree at 80% the family
  only re-ranks the models it has compared (two or more with five evaluated rows each, shift
  at most 0.25); until then, and with `TRET_ROUTER_FAMILY_PRIORS=off`, routing and
  `quality_score` are unchanged. `router_gate` on the family page and the scorecard.
  `TRET_ROUTER_FAMILY_PRIORS`, `TRET_ROUTER_FAMILY_GATE_MIN_CHECKS`,
  `TRET_ROUTER_FAMILY_GATE_MIN_AGREEMENT`. Approvals now drop the priors cache.

- **Batch experiments.** Compare two or three arms (a model, an objective, an iteration limit)
  as one batch per arm over the same rows or one batch split across arms, read the comparison
  with intervals, and let an admin make the winning arm the harness default. A batch also
  accepts an `objective`. Every harness policy or loop change (edit, experiment, revert) is now
  recorded with who, when and the previous value, and an admin can revert it. New
  `/api/batches/experiments` routes, `GET /api/harnesses/{id}/policy-history`, and an
  `experiment` option on `start_batch`. See docs/batch-runs.md.
- **Batch-review lessons with a harness scope.** A batch review's lessons are proposed
  (never approved) as harness-scoped lessons for the batch's harness; once a person approves
  them they are added to every later run of that harness, with or without a pack, except
  evaluator runs. New `GET /api/harnesses/{id}/lessons` and review/retire endpoints; lesson
  rows gain `harness_id`, `scope` and `proposed_by_evaluation_id`; the evaluation detail gains
  `lesson_proposals` (one entry per review lesson, with its outcome and the lesson's current status)
  and `lessons_harness_id`. Proposals go to the harness the batch ran on, retired and rejected
  lessons are not re-proposed, and invisible characters are stripped from lesson text. Migration
  `d9e4b7a1c2f8`.
- **Advice at batch creation, a per-target cost cap and opt-in row memory.**
  `POST /api/batches/advice` recommends a model, iteration limit, cost cap and concurrency
  from the family's earlier evaluated batches (a model needs 5 scored rows; read-only), and
  `start_batch` returns the same suggestion as `advice`. New batch settings: `max_cost_usd`
  (per-target cap, stamped as `_cost_cap_usd`, only ever narrows the harness cap) and
  `row_memory` (off by default; appends a short, labelled block of earlier attempts at the
  same row to each task, from structured fields only: failures appear as one fixed
  category, never error text, and only for the targets being launched). The suggested cost
  cap is never below $0.01; advice can carry a concurrency suggestion with no recommended
  model; advice for an unknown or archived harness is a 422. No migration.
- **A blind pairwise judge, a family leaderboard and a `batch_compare` tool.** Two
  quality-comparable batches can be judged head to head on the rows the rubric cannot
  separate: `POST /api/batches/compare/pairwise` runs one judge twice per pair (outputs
  scrubbed of model, provider, cost and batch identity, order swapped) and records a
  verdict (`a`, `b`, `tie`, `inconsistent`; clear or lean) beside the rubric scores, never
  instead of them. `GET /api/batches/compare` adds `pairwise` per row and
  `summary.pairwise`. New table `pairwise_judgments` (migration `c7d2e9a4b6f1`), new
  evaluator tool `submit_pairwise`; pairwise runs write no `run_outcomes`.
  `GET /api/batches/families` and `/family?key=` serve a per-family leaderboard with a
  per-model aggregate (yield, quality with interval, cost and Wh per acceptable result,
  p95, Pareto). The chat harness gains the read-only `batch_compare` tool, offered once
  to existing chat harnesses that still have the other batch tools.
  The pairwise task is cut at 12,000 characters (rubric 8,000); a second start for a pair
  already in flight is a 409; a judgment whose rubric version is gone fails with "the rubric
  changed; start the pairwise judge again"; an unedited Batch Evaluator doctrine is upgraded
  to mention `submit_pairwise` and an edited one is left alone.

### Changed

- **Each model's most recent batch is shown beside its pooled figure.** Family models and
  the advice evidence carry `latest_batch`, and the advice adds a note when a model's most
  recent batch lies outside its pooled interval. The recommendation rule is unchanged. The
  advice response also returns the family's `harness_id`.

- **Approvals now carry a purpose, and only a review counts as quality evidence.** An
  approval is `review` (the default) or `packaging`; a page map approved only so it can be
  packaged is recorded with `purpose: "packaging"` (`tret batch approve --packaging-only`) and
  no longer counts toward `quality_score` or the human term of `evaluated_quality_score`.
  Rejections are always reviews. Existing approvals default to `review`, so re-scoring changes
  existing scores only for runs whose approvals you re-mark with
  `tret approvals mark-packaging --batch <id> [--dry-run]`. Migration `a7c3e1d94b62`.
- The batch scorecard's `human.decided_reviews` now counts reviews only; page maps approved
  only so they could be packaged are counted separately as `human.packaging_approvals`.
- Experiments never pin a model. Make default applies only an arm's objective and iteration
  limit; an arm's model stays with the router, its evaluated runs being evidence for the
  family gate, and a model-only arm is not promotable. Experiment results gain
  `experiment.router_gate`, and `POST /api/routing/preview` accepts an optional `family_key`
  (workspace taken from the caller) and returns `family_priors` when family evidence applied.
- **The batch comparison has a both-delivered view, and a gap is cross-checked against
  other runs.** `GET /batches/compare` adds `summary.delivered` (wins, ties, mean quality
  and delta over the rows both batches delivered, so low yield no longer drags the
  quality comparison), `both_delivered` and `disputed_gap` per row and
  `disputed_gaps_a` / `disputed_gaps_b`. When the judge scores a gap in a disclosure
  batch, its evidence pack now lists acceptable deliveries of the same company in other
  batches of the family (no batch or model names) so a gap reason that the issuer's own
  site contradicts is caught; the judge still decides. The scorecard adds
  `quality.gaps_disputed` / `gaps_rechecked`, target briefs `delivered_elsewhere` /
  `gap_rechecked`, and "Only unscored" re-judges a scored gap when a new delivery elsewhere
  appears or a shown one stops qualifying. No rubric change.

- **A delivered row is cross-checked against the main documents other runs packaged.** A
  run that packaged only companion PDFs (an index, a scorecard, a glossary) was scored 93
  like runs that packaged the real report, because a register note said no main PDF
  existed. When the judge scores a delivered target of a disclosure batch, its evidence
  pack now lists main documents (`primary_disclosure`, `embedded_statement_and_assurance`)
  that acceptable deliveries in other batches of the family packaged and this run did not
  (by sha256 or normalised URL; no batch or model names) and tells the judge to cap
  right_document at 2 with `missed_primary` when only companions were packaged; the
  register notes are labelled as possibly out of date. A main-role source under 25% of
  the pages of this run's largest main-role source and under 20 pages is not counted. The judge still
  decides. The scorecard adds `quality.primaries_disputed` / `primaries_rechecked`
  (delivered rows, not documents), target briefs
  `primary_elsewhere` / `primary_rechecked`, the comparison `disputed_primary` per row and
  `disputed_primaries_a` / `_b`, and "Only unscored" re-judges a scored delivery when
  a new document appears or a shown one stops qualifying (not when another run delivers a
  document already shown; a document is the same while its hash or normalised URL
  overlaps). No rubric change.

- **Routing prefers open-weight models and sees the live OpenRouter catalog.** Every
  model carries `open_weights` (from OpenRouter's `hugging_face_id`, or `models.yaml`;
  local models are open) and a harness policy can say `open_weights: prefer | require |
  any` (default `TRET_ROUTER_OPEN_WEIGHTS=prefer`: evidence first, then open before
  closed, then the objective; `any` is the old ordering byte for byte; an invalid
  value now stops startup). The live OpenRouter catalog can load without a global
  key when `TRET_OPENROUTER_CATALOG_PUBLIC_FETCH=true` (default false, so a keyless
  self-hosted install still never calls openrouter.ai), refreshes in the background
  every 6 hours (`TRET_OPENROUTER_CATALOG_TTL_SECONDS`, one shared download), and
  brings OpenRouter's zero-data-retention endpoint list: under ZDR provider prefs a
  model with no ZDR tool-capable endpoint (after `ignore`/`only`) is not routed to,
  and a list that matches too few curated models is treated as unknown. Uncurated models reach the router only
  through the new-model seats (6, newest per lab, 120 days, specialist variants
  skipped, open-weight first, same context-window check as everything else), by
  being named in the harness's `allowed` list, or by earning a non-poor record;
  exploration now runs under `balanced` and `eco` on extraction and freeform tasks
  (never for the SDK or `tret run`, which keep no outcome record). Routing prompt
  `route-v7`. See [docs/upgrading.md](docs/upgrading.md).
- The Disclosure Collector harness no longer seeds a `min_cost_tier: standard` floor
  (eco objective, standard ceiling kept; an unedited harness is upgraded at boot).
  The per-run web-search cap now stops the search loop the floor guarded against, and
  price tiers excluded nearly every open-weight model regardless of capability.
- **A run stuck on one failing call now stops early.** When a model makes the same
  failing tool call, with the same arguments and the same error, N times in a row, the
  run ends with `stopped: N identical failing calls to <tool>` instead of running on to
  `max_iterations`. The call before the stop warns the model. A model switch starts the
  count again, a run that already recorded its terminal result keeps it, and a disclosure
  target that already has a package map is still settled by its maps.
  `loop_config.max_identical_failures` sets N (default 3, minimum 2; 0 turns it off).
  Outcome records classify the stop as `repeated_failing_call`. A batch-evaluator judge
  stopped this way gets one relaunch, as one that ran out of turns already did. In the
  2026-11-09 open-weight comparison, Kimi K3 ran 15 of 48 disclosure targets to the
  40-iteration cap this way.
- **`propose_package_map` explains `keep_ranges` with an example.** A malformed value
  now gets an error that shows a valid one (`[[12, 40], [96, 98]]`) and says what was
  received. A flat, even-length list of integers, which some hosts produce by flattening
  the nested list, is read as consecutive pairs. The tool result says how it was read,
  and the draft still waits for a reviewer's approval.
- **`propose_package_map` shows what the chosen pages hold.** A model that copies the
  page numbers printed in a report's contents page into `keep_ranges` selects the wrong
  pages, because those are printed numbers and the ranges are positions in the PDF. The
  result now quotes the opening words of the first and last page of every range and,
  where the page's own text carries one, its printed number. It recognises a constant
  shift between printed and physical numbering and files laid out as two printed pages
  per PDF page, and says so. A range that runs past the end of the file is told the
  number is probably a printed one. The tool still never corrects pages itself: every
  number it states is read from a page's text. The Disclosure Collector's procedure
  carries the same warning.

### Fixed

- **Two causes of flaky tests.** The PostgreSQL `UUID` column type now compiles to
  `CHAR(32)` on SQLite (tests only): the type name `UUID` got NUMERIC affinity there, so a
  uuid4 made only of digits came back as a float. And `tests/conftest.py` restores the
  batch module's bound app after every test, so a lifespan test no longer leaves later
  tests with a draining app that refuses to launch targets.

- **A search-backend outage no longer counts against the model.** `web_search` now reads
  SearXNG's `unresponsive_engines`: when a query returns nothing and two or more engines did
  not respond, the tool tells the model the backend may be unavailable, and each call's audit
  row records which case it was (`search_results`, `search_empty`, `search_degraded`,
  `search_unavailable`, `search_unconfigured`). A batch target whose run delivered nothing
  while every search that reached the backend failed this way (at least two) is marked
  `search backend unavailable: ...` at settlement and classed `infra`: out of the yield,
  never judged, retried like other infrastructure failures. The model's own report of
  "blocked" does not decide this, a timed-out target stays `timeout`, and a search that was
  never configured is not an outage. The batch page says how many targets were affected
  (`search_outage_targets`). `tret batch search-outages` lists earlier runs whose every
  search returned nothing and marks the ones an operator names; a marked row that was
  already judged stops counting, and a human review on it is kept.

- **`MissingGreenlet` in runs that searched documents another run was chunking.**
  `ensure_chunks` rolled back the run's whole session when a concurrent run won the
  `UNIQUE(document_id, ordinal)` race, expiring the run row mid-loop. Chunks are now
  inserted under a savepoint (a lost race is "already chunked"), and the harness
  reloads an expired run after a successful tool.
- **Thinking models lost their reasoning between tool turns.** OpenRouter
  `reasoning_details` (or the `reasoning` string; `reasoning_content` on the Kimi
  adapter) is kept on the assistant turn and replayed unmodified on later requests to
  the same model, as Kimi K3 and DeepSeek V4 require for tool loops.
- **Tool argument errors name the accepted parameters.** An unknown argument gets a
  did-you-mean and the full accepted list (required marked), unparseable JSON
  arguments say so, and the same failing call repeated three times gets a pointed
  reminder that also counts toward supervisor escalation. A live Kimi K3 run had
  repeated one misnamed argument 36 times. Non-object arguments are a tool error,
  not a crashed run, and a provider that rejects replayed reasoning gets the call
  once more without it.

- The Disclosure Collector harness now seeds routing objective `eco` within its standard-tier floor (an unedited harness is upgraded at boot): on the same 12 registers, `eco` (GPT-5.6 Luna, low effort) wrote 12/12 like `balanced` (GPT-5.6 Terra) at about a quarter of the cost and energy.

### Added

- **Batch scorecards, comparison and an evaluator.** Every batch gets a scorecard (yield,
  cost and Wh per acceptable result, reliability, outliers; infrastructure failures and
  timeouts never count against the model) and two batches of the same task can be compared
  row by row (`GET /api/batches/{id}/scorecard`, `GET /api/batches/compare`). Optionally a
  judge model scores each finished target against a rubric from a blind evidence pack, with
  a budget, relative scores, a batch review and human spot checks (`evaluation` on create,
  `POST /api/batches/{id}/evaluate`). The judge is named per batch or is the first entry of
  `TRET_BATCH_EVALUATOR_JUDGE` whose provider has a key (default Claude Opus 5.5, then
  Gemini 3.8 Flash for installs with only an OpenRouter key). See
  [docs/batch-runs.md](docs/batch-runs.md#scorecards-comparison-and-the-evaluator).

- **View a batch target's written files in the app.** On the Batches page, each file under
  a target's **Written files** opens in a large in-app viewer (previous/next across the
  target's files, Download, link to SharePoint): Markdown rendered with a source toggle,
  PDFs embedded, JSON as a collapsible tree with table views for `page_map.json`,
  `package.json`, `extraction_stats.json` and `links.json`. Served by the new
  `GET /api/batches/{id}/targets/{target_id}/files?path=` from the stored copy; `path`
  must match one of the target's `written_files` exactly, and text over 5 MB is
  download-only. See [docs/batch-runs.md](docs/batch-runs.md#viewing-written-files).
- **Attach documents from the chat composer.** A paperclip button and drag-and-drop
  on the composer upload files to the project's documents and attach them to the next
  turn (`document_ids` on `POST /api/chat/{id}/messages`); the model's user turn lists
  each attachment's id and filename so `read_document`, `search_documents` and
  `start_batch`'s `register_document_id` can use them, and the user message shows the
  attachments as chips linking to the Documents page. A turn with attachments is
  attached-only for document tools; the next turn without any is project-wide again.
- **Batches from chat and the command line.** The chat harness gains `start_batch`,
  `batch_status` and `batch_results`: a turn can start a durable prompt batch over
  inline rows or a CSV document, read how it is going and synthesise its results later,
  with a card in the chat linking to the batch. The batch's spend is added to the
  starting run's `delegated_cost_usd` as its targets settle; starting one from a chat
  needs the approver role. `tret batch create | start | status [--watch] | release |
  approve | finalize | list` drives the same API from a shell
  (`--url`/`TRET_URL`, `--token` or `--email`/`--password`, `--workspace`, `--json`). See
  [docs/batch-runs.md](docs/batch-runs.md).
- **Batches, with a generic prompt profile.** The Collections page and API are now
  Batches (`/api/batches`; `/api/collections` stays as a deprecated alias for one
  release). A new `prompt` profile runs any non-archived harness over the rows of a
  CSV: a template with `{column}` placeholders, one run per row, review off by
  default (or pause each target at its first draft finding of a chosen type), and
  three record files (`manifest.json`, `results.jsonl`, `results_summary.md`) that go
  to a SharePoint write target when one is set and are otherwise kept as Documents
  with a download route. Starting or releasing a batch now checks free space on the
  local capture store (`TRET_BATCHES_MIN_FREE_MB_PER_TARGET`, default 50 MB per
  target; 0 disables). The disclosure collection behaves as before. See
  [docs/batch-runs.md](docs/batch-runs.md).
- **Object storage for document bytes.** `TRET_STORAGE_BACKEND=s3` keeps uploads,
  fetched snapshots, imported SharePoint/Drive files and packaged page subsets in an
  S3-compatible bucket (Fly Tigris, AWS S3, MinIO) with a bounded local cache, so
  they survive a restart, a redeploy and a full volume. The default `local` backend
  is unchanged on disk. New `documents.storage_key` column (migration backfills it),
  `tret storage status` and the resumable `tret storage migrate --to s3`, and an
  optional `tret[s3]` extra (boto3). See [docs/storage.md](docs/storage.md).
- **Disclosure collection.** A run can now collect a public sustainability
  disclosure and keep a verified page subset of it, with a person approving every
  page decision. See [docs/disclosure-collection.md](docs/disclosure-collection.md).
  - `capture_source` stores the complete original of a public file (an issuer PDF,
    a workbook) as a `source_kind='web'` Document with its SHA-256, retrieval time,
    redirects, headers and a changed-source warning when `expected_sha256` does not
    match. A file over `TRET_EGRESS_CAPTURE_MAX_BYTES` (60 MiB) is refused rather
    than truncated, and `TRET_EGRESS_CAPTURE_MAX_PER_RUN` bounds attempts per run.
  - `propose_package_map` records a draft `package_map` finding: which physical
    pages of a captured PDF form the disclosure, with explicit ranges for embedded
    statements, incorporated sections recorded and retained in full, and exclusions
    with reasons. It packages nothing.
  - Approving a `package_map` runs the deterministic packager (Poppler `pdftotext`,
    with a `pypdf` fallback recorded as such): the retained PDF, a page map, a link
    audit, layout and raw page-referenced text, extraction stats and a package
    record, each registered as a `source_kind='package'` Document. The source is
    re-hashed first, output is verified before it is published, a failure leaves
    nothing behind, and `POST /api/findings/{id}/package-retry` reruns a failed
    package. `poppler-utils` is now in the Docker images.
  - `propose_connected_write` accepts a `document_id`, uploading a stored Document
    unchanged (up to 25 MB) to a relative path under the target's `tret/` folder,
    pinned to the SHA-256 the approver saw.
  - **Collection runs.** A register of target companies (CSV or JSON, up to 500
    rows, with warnings when the sample is thinner than the briefing asks) is
    collected one engine run per target on the Disclosure Collector harness.
    New `collections` and `collection_targets` tables hold the register and each
    target's run, outcome, page-map findings and uploaded files; the target
    status is re-derived from those facts, never set by hand. `/api/collections`
    (create, list, detail, start, pause, resume, release, per-target retry and
    retry-write, a records preview, finalize) is read-open to members and
    approver-only for every change, and the Collections page drives it: a
    first-batch gate with Release, a review queue of draft page maps, and a
    per-target table. New tool `report_collection_outcome` is how a target's run
    scores itself (captured, substituted, gap or blocked, with the dates,
    framework and URLs it established). `POST /api/findings/decide-batch`
    approves or rejects several findings in one request, each decided as the
    single approval would be. With auto-write on, approving a map uploads that
    package's files under the write target's `tret/` folder; finalize writes
    `manifest.json`, `execution.json`, `exception_log.json`,
    `target_register.json`, `SHA256SUMS.txt` and `results_summary.md`.
    Uploads replace, attributed to
    each map's approver; finalize can be repeated (it overwrites the six files,
    `SHA256SUMS.txt` last) and is refused while targets are busy or maps are
    undecided. Retry covers `failed`, `gap`, `rejected` and `write_failed`, keeps
    earlier attempts and their cost in `history`, and a run that failed but proposed
    maps is judged by them. Collection status: running, then review, then gate, then
    completed. Settings `TRET_COLLECTIONS_MAX_CONCURRENT` (3) and
    `TRET_COLLECTIONS_RUN_TIMEOUT_SECONDS` (1200). A restart re-settles in-flight
    targets (a killed run becomes failed) and pauses a collection that still has
    pending targets for an operator to resume; no target launches during shutdown. See
    [docs/disclosure-collection.md](docs/disclosure-collection.md#6-collection-runs-batches).
  - `TRET_EGRESS_CAPTURE_ANY_PUBLIC_HOST` (default off): `capture_source`, and
    only it, may fetch an original from any public https host, because issuers
    serve reports from CDNs nobody can allowlist ahead of time. `fetch_url` and
    `web_search` stay behind the allowlist; the address, https, port, size and
    PDF checks are unchanged.
  - The capture ceiling `TRET_EGRESS_CAPTURE_MAX_BYTES` now defaults to 60 MiB
    (was 25 MiB).
  - `upload_connected_file` takes an optional `max_bytes`; a collection uploads
    package files under the capture ceiling instead of the 25 MB import cap.
- **Archive a team.** `POST /api/workspaces/{id}/archive` (owner only, team
  workspaces only, the workspace name typed back as confirmation) soft-deletes
  a workspace: `workspaces.archived_at` is set, pending invites are revoked,
  the workspace drops out of every membership list, switch and invite links
  to it refuse, and its name may be reused. Every row in it is kept.
  Settings gains an "Archive this team" panel for owners. Extensions may
  refuse through the workspace gate with action `"archive"`.
- **`users.email_verified`.** A real column replaces the "has a password
  hash" stopgap that gated pending-invite listing and invite redemption. It
  follows the IdP's `email_verified` claim on every OIDC login, is true for
  administrator-created password accounts, and the migration backfills true
  for existing password accounts.
- **Stop works on a wedged run.** The engine now knows which runs this
  process is executing. Cancelling a run that is queued or running with no
  live task behind it (crashed task, missed by the startup sweep) marks it
  failed after a 30-second grace period, ends any open event stream, and
  frees the conversation it was blocking. The cancel response reports
  `{ok, live, marked}`.
- **Error boundary.** A render error in one view shows "This page hit an
  error" with Try again and Reload, leaving the sidebar and workspace
  switcher usable, instead of blanking the whole app.

### Changed

- **Disclosure collections from chat; large-PDF extraction and packaging limits;
  Disclosure Collector cap $8.** `start_batch` takes `profile: "disclosure"` with a
  register CSV (inline or an attached document) and a write target, and the chat card
  shows the profile and any register warnings. The text-extraction budget's cap rises to
  600 s (a 600-page registration document is no longer cut off), and packaging's source
  check reads only the retained page ranges and is skipped with a recorded warning when
  Poppler fails on the source, instead of failing the target. The seeded Disclosure Collector's per-run
  cost cap is now $8 (was the $5 engine default); an unedited harness is upgraded at boot,
  an edited one keeps its own. See [docs/batch-runs.md](docs/batch-runs.md).
- **Collection runs now sit on a generic batch orchestrator** (`tret/batches/`,
  with a `BatchProfile` seam; the disclosure code moved to `profiles/disclosure.py`).
  `/api/collections`, prompts and record files are unchanged; a migration adds
  `profile`, `harness_id`, `parent_run_id` and per-target `label` / `task_input`.
  Finalize now returns 409 while a target failed on an I/O or storage error (send
  `{"force": true}` to override), and create warns about known-URL hosts the
  research allowlist would make `fetch_url` refuse.
- **Collection runs stop searching when they cannot fetch.** `web_search` is capped
  per run (`TRET_EGRESS_RESEARCH_MAX_SEARCHES_PER_RUN`, default 12), and a fetch or
  capture refused by the research allowlist now says the host is not allowlisted,
  that searching again will not help, and that an operator must add it; the search-cap
  error names the hosts refused that run.
- **Routing floor.** `model_policy.min_cost_tier` (default none) excludes models
  below a tier on every automatic routing path, the mirror of `max_cost_tier`. The
  seeded Disclosure Collector now requires the standard tier, and an unedited
  collector with the previous seed policy is upgraded at boot.
- Energy figures are labelled by what they are: the headline **Energy** is
  facility energy (IT energy × PUE, the basis for the carbon figure); **IT
  energy (before PUE)** is shown as the secondary figure; a run still
  streaming shows **live estimate**, a finished run **final**. The Emissions
  dashboard uses the same labels. No computation or stored value changed.
- **OpenRouter requests are zero-data-retention by default.** With
  `TRET_OPENROUTER_PROVIDER_PREFS` unset or blank, every OpenRouter call now
  sends `provider: {"zdr": true, "data_collection": "deny", "ignore":
  ["moonshotai"]}`, and the deterministic fallback table no longer lists
  `openrouter/moonshotai/kimi-k3`. An explicit value replaces the default
  (`{}` sends nothing). Direct `kimi/*` models still run, on your own
  `TRET_MOONSHOT_API_KEY`. See [docs/hardening.md](docs/hardening.md) §10 and
  [docs/upgrading.md](docs/upgrading.md).

### Fixed

- **Confirmation prompts no longer depend on `window.confirm`,** which some embedded
  browsers (such as the Claude desktop app's browser pane) suppress, so Finalize, Clear
  overrides and the pack-builder deletes silently did nothing there. They now open an
  in-app dialog: Enter confirms, Escape cancels, and focus returns to the button.
- **S3 cache eviction thrash.** With the cache cap below the never-evicted floor, every
  `put` or `local_path` unlinked every other blob older than 60 s and concurrent readers
  lost files ("The stored source file is missing"). Packager outputs under `packages/`
  are now ordinary cache after the run (pinned only while it runs), eviction accounts
  the cap over evictable files and re-checks each file before unlinking, writers
  re-create a pruned directory, `local_path` re-downloads a file that vanished
  underneath it, and `tret storage status` reports the pinned/in-progress floor.
- **Collections page: Max iterations field.** The New collection form now sends
  `max_iterations` (1–50) as the docs described, and the collection header shows the cap.
- The OAuth connection callback refuses a workspace that was archived after
  the authorize step.
- The public-address check on outbound requests now refuses carrier-grade NAT
  addresses (100.64.0.0/10), which the earlier private-range list missed. It
  applies to every egress class, not only captures.
- **Lessons from the first live 12-company collection run.** `propose_package_map`
  refuses a stem another map in the same run already uses, the collection prompt
  asks for `<slug>-container-<topic>` stems, and a collection refuses to write two
  packages whose file names collide (three HSBC methodology documents had shared one
  stem and replaced each other on SharePoint). A captured file is written atomically
  and an existing content-addressed copy is kept rather than rewritten (a copy owned
  by another user had made the capture fail). Text extraction of a captured PDF over
  2 MB keeps its 180 s wait and grows beyond it with size (30 s plus 4 s per MiB,
  at most 240 s), and Poppler is now allowed the same time instead of being
  stopped at 120 s while the caller was still waiting, which is what left 15 MB and
  48 MB annual reports with no text. Upload-session chunks
  to Microsoft Graph get 180 s instead of 60 s, and a transport error without a
  message is named by its type. Target slugs transliterate ø, æ, ß and friends and
  drop a trailing "and" (Ørsted, "& Co."). `results_summary.md` carries the blocked
  or gap reason the run reported.

## [0.2.0] — 2026-10-15

### Added

- **Verdicts you can read.** Approvals and the run page render a
  `divergence_verdict` as a card: verdict, reason code, confidence, the
  methodology note and the cited dataset values, with the raw payload behind
  a fold and every retrieved value behind another. The run page lists each
  finding a run recorded with its status. `record_verdict` now tells the
  model what it recorded and asks it to state the verdict in its answer.
- **One verdict per case.** A new draft `divergence_verdict` for the same
  site and peril supersedes the older draft in the same project (status
  `superseded`, cross-linked through `supersedes` / `superseded_by` on the
  finding). Approved and rejected findings are never touched. Verdicts of
  `insufficient_data` are stored with status `noted`: they stay on the run
  and in `GET /api/findings`, but leave the approval queue, since the data
  request filed with them is the actionable item. The portfolio divergence
  rollup skips superseded rows.
- **Chat stop and resume.** A Stop button cancels the reply in flight.
  `GET /api/chat/{id}` reports `active_run_id` (the newest live top-level run
  on the conversation, under an hour old); the page re-attaches to it after a
  reload instead of forgetting the run, and a second send while a reply is
  live is refused with 409 rather than starting a second paid run.
- **Invites that explain themselves.** `POST /api/auth/invites/{token}/accept`
  returns 200 and switches to the team when the caller already redeemed that
  invite (it used to say "invalid or expired"), 410 with a reason for a
  revoked or expired invite, and 404 only for an unknown token or someone
  else's invite. `GET /api/auth/invites/pending` lists the caller's own open
  invites, and the first-run screen offers to accept one, or to switch to a
  team the user already belongs to, before it offers credits.
- **Routing errors that say why.** "No candidate models" now reports how
  many catalog models were considered and why each was excluded (no provider
  key per provider, above the policy's cost tier, not in the allowed list,
  no tool support), and logs the same breakdown.

- **Water consumption.** Every new run records an estimated water figure next
  to its carbon: on-site cooling water (IT energy × site WUE, default 0.375
  L/kWh from LBNL 2024; 0 for local runs) plus power-generation water
  (facility energy × grid water factor, default 4.81 L/kWh, the WRI 2020 world
  average, hydro evaporation included). Consumption basis only, with a ÷3/×3
  judgment band. Stored as `energy_accounting.water`; returned by
  `GET /api/analytics/emissions`, the what-if endpoint and deliverable exports;
  configurable through a `water` block in the workspace emissions settings and
  `TRET_WATER_*` env vars; shown on the run page, the Emissions dashboard
  (Water view), the what-if drawer and the settings. Runs recorded earlier
  show "Not recorded", never 0. Method and sources:
  [docs/water-methodology.md](docs/water-methodology.md).
- **Per-upstream water disclosures.** A settings layer may carry
  `water.upstreams.<google|aws>` cooling-water figures, selected per call by
  the same `served_by` identity as per-upstream PUE. Entries must declare
  `water_basis: "consumption"` and `denominator: "it_energy"`, so a
  withdrawal-basis WUE cannot be entered as one. What-if scenarios that name no
  water keys now keep each run's recorded water factors.
- **Method v3 preview.** Every server-side run now also records a parallel
  emissions estimate under the revised `facility_v3` method (Kith method lab,
  2026-10-01), stored additively in `runs.routing["method_v3"]` and returned as
  a new optional `method_v3` field on run detail (`GET /api/runs/{id}`, slim;
  `?v3=full` for the whole block) and on the chat turn payload, null for runs
  that predate it. It is labelled a preview and never replaces `co2e_g`,
  `energy_accounting`, cost, analytics or exports, none of which change; the
  run `routing` field is returned without it. The run receipt shows it as a
  small secondary line.
- **New optional evidence keys.** Overhead (router / compaction) call records
  gain `served_by`, and per-call records in `model_timeline` gain
  `reasoning_requested`; both are additive and absent or null on older runs.
  Curated catalog entries may carry an `openrouter_id`.

- **TypeScript SDK (`@tret/sdk`, `sdk/typescript/`).** A zero-dependency
  client for a running tret server, for Node ≥ 18, Electron and the browser
  (ESM + CJS + types). It covers auth (bearer token, session cookie or none),
  harnesses, packs, documents, runs, findings and `version()`. `runs.events()`
  streams a run as a typed `AsyncIterable` of every event the engine
  publishes, with its own SSE parser over `fetch` that reconnects on its own.
  `runs.complete()` creates a run, streams it to the end and returns the run,
  its findings and a `Receipt` with the Python SDK's fields; `null` there
  means unavailable, never zero. Errors are typed: `TretAuthError`,
  `TretNotFound`, `TretRateLimited`, and `TretBudgetRefused` for gate
  refusals, including a run refused by the pre-run gate. Request/response
  types are generated from a committed OpenAPI snapshot, which CI regenerates
  (`backend/scripts/dump_openapi.py`, no database needed) and diffs. Not on
  npm yet; [docs/embedding.md](docs/embedding.md#typescript) and the package
  README cover installing it from a checkout. A reconnect always goes back to
  the stream before it reads the run record, so a run that finished while
  the connection was down still delivers its trailing events. A 2xx that is
  not JSON (an SPA shell) is a `TretError`, not a parse crash.
  `complete()` asks for up to 500 findings, the server's cap.
- **`GET /api/version`** returns `{version, git_sha}`. It is unauthenticated,
  like `/api/healthz`. The sha comes from `TRET_GIT_SHA` (set it when
  building an image), else from `git rev-parse HEAD`, else `null`. The `git`
  answer is used only when the repository's top level is the tret checkout
  itself, and it is read off the event loop, once per process.
- The OpenAPI schema now lists the two accepted `action` values on
  `POST /api/findings/{id}/approval`. The body (`{action, note}`) and its
  validation are unchanged.

### Changed

- **A session always has a workspace.** A browser session for a user with
  several memberships lands on a default workspace (the cookie's workspace if
  still a membership, else the most recently joined team, else the first
  membership) instead of a 409 that left every workspace call failing until
  the user picked one. Bearer (API) callers keep the explicit contract: a
  missing or wrong `X-Tret-Workspace` header is still a 409.
- Password login redeems pending invites the way OIDC login does.
- Creating a team whose name matches one the caller already owns is a 409.
- Switching, creating or joining a workspace updates the UI in place; it no
  longer needs a reload.
- Findings carry two new statuses, `noted` and `superseded`, next to `draft`,
  `approved` and `rejected`. Clients that filter by status should expect
  them; neither can be approved.

### Fixed

- Saving the emissions settings form no longer deletes settings the form does
  not show (`energy_strategy`, `model_overrides`, `pue.upstreams`, grid rows for
  other providers or regions, the embodied GPU model). The form previously
  rebuilt the whole document on save.
- Tenancy isolation tests now also cover run children, run events, method
  runs and deliverable exports.

## [0.1.0] — 2026-09-29 — first public release

tret, an open-source AI workbench: a harness platform for running LLM agents
against real work with an audit trail, approvals, and an honest cost line.

### Added

- **Workbench and chat.** Chat-first UI with runs, run audit, approvals,
  delegated sub-runs, and a harness builder; harnesses link one or more domain
  packs.
- **Multi-provider routing.** OpenRouter, Anthropic, OpenAI-compatible and
  local model servers behind one interface, with an LLM router, routing
  preview (model choice and cost range before a run starts), bounded
  exploration, prompt caching, and per-run budgets.
- **Delegation.** `delegate_parallel` and `spawn_subagent` tools with limits,
  retries, and cancellation, shown as child runs in the UI.
- **One door to the internet.** A single egress chokepoint (`TRET_EGRESS`),
  optional web research through a bundled SearXNG, and zero-cloud operation
  with a local model.
- **Ecological cost line.** Per-run energy and emissions estimates with a
  layered factor model (workspace overrides, grid zones, what-if recompute);
  methodology in [docs/emissions-methodology.md](docs/emissions-methodology.md).
- **Grounding check.** Chat replies flag numbers that don't trace to retrieved
  data or the conversation.
- **Domain packs.** Pack format, validator, archive import/export, an in-app
  pack builder, and a registry client; ships a climate-risk pack.
- **Connections.** Microsoft 365 and Google Drive connectors
  ([docs/connections.md](docs/connections.md)).
- **Security and operations.** OIDC and password login, workspaces and roles,
  production boot checks, security headers, single-instance lock, non-root
  container image, and a hardening guide ([docs/hardening.md](docs/hardening.md)).
- **Install and deploy.** One-command installer, Docker Compose stack, and
  templates for Fly.io and Render.
- **Telemetry, off by default.** Optional anonymous aggregate usage reports
  that an admin must opt in to ([docs/telemetry.md](docs/telemetry.md)).
- **Approve all sections.** Deliverables, and the Approvals page for a drafted
  section, offer "Approve all N draft sections" for one deliverable after an
  inline confirm. Each section still gets its own named approval, tagged as
  coming from the batch action. Only each section's latest draft is approved,
  and a section whose draft changed after you confirmed is skipped.
- **Pending counts in the sidebar.** Approvals shows how many findings are
  waiting for a decision, and Deliverables how many deliverables have sections
  still awaiting approval. Both refresh every 30 seconds and immediately after
  a decision.

[Unreleased]: https://github.com/kith-ai-lab/tret/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kith-ai-lab/tret/releases/tag/v0.2.0
[0.1.0]: https://github.com/kith-ai-lab/tret/releases/tag/v0.1.0
