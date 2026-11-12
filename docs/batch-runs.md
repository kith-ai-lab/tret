# Batch runs

A **batch** runs one harness task per row of a table, a few at a time, and keeps
track of every row: which run it started, what the run answered, what it cost and
whether a person still has to decide something. It adds bookkeeping and
parallelism. It adds no new authority: whatever a harness could do in one run, it
can do in each of these, under the same tools, budgets and approvals.

The page is **Batches** (`/batches` in the app; the old `/collections` URLs
redirect). The API is `/api/batches`; `/api/collections` is the same API under its
old name, marked deprecated, kept for one release.

## The two profiles

A batch has a **profile** that decides what a row is and what happens after a run
finishes.

| | `disclosure` | `prompt` |
|---|---|---|
| A row is | a target company (market, category, known URLs...) | any row of a CSV |
| Harness | the Disclosure Collector | any non-archived harness in the workspace |
| The run is asked | to collect the company's sustainability disclosure | your template with the row's cells filled in |
| Review | package maps, always on | off by default; one finding type of your choice when on |
| After a run | approved maps are packaged and uploaded | nothing |
| Records | six files, to SharePoint | three files, to SharePoint or kept as Documents |

The disclosure profile is documented in
[disclosure-collection.md](disclosure-collection.md#6-collection-runs-batches) and is
unchanged. The rest of this page is about the `prompt` profile, plus what every
batch shares.

## Creating a prompt batch

`POST /api/batches` with `"profile": "prompt"`:

```jsonc
{
  "profile": "prompt",
  "name": "Supplier screening Q4",
  "run_slug": "supplier-screen-q4",          // 3-61 chars: lowercase letters, digits, hyphens
  "harness_id": "<uuid>",                    // required
  "template": "Assess {company} ({country}) against our supplier code. Reply with a verdict and two sentences.",
  "csv": "company,country\nAcme Ltd,DE\nGlobex,FR\n",   // OR "rows": [{"company": "Acme Ltd", "country": "DE"}, ...]
  "label_column": "company",                 // optional; default: the first column
  "slug_column": null,                       // optional
  "review_schema": null,                     // null = review off
  "concurrency": 3,                          // 1-6 runs at once
  "first_batch_size": 0,                     // 0 = no gate
  "target_slug": null, "write_root": null,   // optional SharePoint write target (both or neither)
  "model_override": null, "max_iterations": null
}
```

The response is the batch detail (the same JSON as `GET /api/batches/{id}`), with
`warnings` from parsing. Provide exactly one of `rows` and `csv`, with 1 to 500 rows.
Errors are `422` and the message names the field.

### Worked example

```csv
company,country,note
Acme Ltd,DE,"first, with comma"
Globex,FR,
Initech,US,third
```

With the template

```text
Assess {company} ({country}). Context: {note}. End your reply with {{verdict: ok|flag}}.
```

row 1 becomes the task message

```text
Assess Acme Ltd (DE). Context: first, with comma. End your reply with {verdict: ok|flag}.
```

and the batch has three targets labelled `Acme Ltd`, `Globex`, `Initech` (the first
column), with slugs `row-1`, `row-2`, `row-3`. Row 2's empty `note` is reported as a
warning (`1 row has an empty 'note'`), not an error: it renders as nothing.

### Templates

- `{column}` is replaced by that row's cell. Every placeholder must be a column of
  the data, or create answers `422` listing the missing ones. Placeholders are plain
  column names: `{a.b}`, `{a[0]}` and `{}` are refused.
- **A literal brace is written `{{` or `}}`**, as in Python's `str.format`. A lone
  `{` or `}` is a `422`.
- The template is stored with the batch and cannot be edited afterwards, so every
  row renders the same way. Cells are trimmed and capped at 20,000 characters.

### Labels and slugs

- The label is the cell in `label_column` (default: the first column), trimmed. An
  empty cell is labelled `row N` and counted in a warning. Duplicate labels are made
  unique (`Acme #2`) and counted in a warning.
- The slug names the target in logs and run labels: `row-N` by default, or, when
  `slug_column` is set, that cell (or the label, if the cell is empty) lowercased to
  ASCII letters, digits and hyphens, at most 40 characters, with a numeric suffix
  when it repeats.
- A row whose cells are all empty is skipped as a blank line. Columns may not repeat
  or be unnamed, and a CSV row may not have more cells than the header.

### Review

By default a finished run is simply `completed`: the batch is a way to run many
prompts, and the answers are in the results. Setting `review_schema` makes each
target pause in `review` while its run has a **draft finding** that the batch
reviews:

- `"any"`: a draft finding of any type the run proposes. This is the usual choice
  for "a person approves what the harness wants to do", whatever the harness is.
- A finding type by name: only drafts of that type pause the target, for example
  `"connected_write"` (a proposed write to a connected source) or a pack's finding
  type such as `"risk_statement"`.
- `null` (the default): review is off.

The decisions are the ordinary ones (`POST /api/findings/{id}/approval`, or
`/api/findings/decide-batch` for several). When every draft of a target is decided
the target becomes `completed`, or `rejected` if all of them were rejected. A target
with no finding of that type is simply `completed`. The detail's `pending_reviews`
lists the drafts (`pending_maps` is the same list under its old name, for one
release). A batch with a pending review cannot be finalized.

Both decision endpoints take an optional `purpose`: `review` (the default) or
`packaging`. Only a review is quality evidence about the model that produced the
finding. `packaging` exists so a disclosure page map can be approved just to be packaged
without inflating that model's record; it is valid only with `approve` and only for a
`package_map` finding. `decide-batch` refuses the whole request when `packaging` is sent with
`reject`, and refuses a finding that is not a page map on its own, leaving the rest of the
request to proceed. A packaging
approval otherwise behaves exactly like any approval. The scorecard counts the two apart:
`human.decided_reviews` counts reviews only and `human.packaging_approvals` counts the
packaging-only approvals. For a batch whose maps were already
approved as reviews, `tret approvals mark-packaging --batch <id> [--batch <id> ...]
[--dry-run]` (run where the database is reachable) re-marks them as `packaging` and
re-scores the affected runs, printing per model the before and after mean `quality_score` and
mean `evaluated_quality_score` (the value batch-family routing evidence reads; `-` when the model
has no evaluated rows).
It is safe to re-run: a second run changes no approvals and re-scores the same runs again,
which repairs a re-score that failed the first time (failed runs are listed and the command
exits non-zero). `--dry-run` prints what would change and the current means only. The command
clears only its own process's priors cache, so a running server picks up the re-scored
records within its priors cache lifetime (about 60 seconds).

### What a target holds

A prompt target's `task_input` is the row (`{column: value}`). Once its run has
finished, `outcome` is

```json
{"answer": "<the run's final assistant message>",
 "finding": {"id": "...", "schema_slug": "connected_write", "status": "draft", "payload": {}},
 "reported_at": "2026-11-05T20:00:00+00:00"}
```

`answer` is the last assistant message with text in the run's own messages (the same
rule the chat view uses for a reply). `finding` is the first review finding when
review is on, else the run's most recent finding of any type, else `null`. Target
status for a prompt batch is one of `pending`, `held`, `running`, `review`,
`completed`, `rejected`, `failed`.

## The gate, pause and retry

These work for every profile. With `first_batch_size` N, only the first N rows run;
the batch then waits in `gate` until an approver releases the rest
(`POST .../release`). `pause` stops launching new runs, `resume` continues, and a
`failed` or `rejected` target can be retried (`POST .../targets/{id}/retry`), which
archives the earlier attempt in the target's `history`.

## Viewing written files

A disclosure target's `written_files` (the package files uploaded to SharePoint) can be
opened from the Batches page: click a file under **Written files** and it opens in a
large in-app viewer with previous/next across the target's files. Markdown is rendered
(with a source toggle), PDFs show in an embedded viewer, and JSON is pretty-printed as a
collapsible tree, with table views for the known shapes: `page_map.json` (retained vs
source pages and the collapsed source page ranges, for checking a package map such as
11 of 100 pages), `package.json` (the packaging record: keep ranges, scope, artifacts,
limitations, verification), `extraction_stats.json` as key/values and `links.json` as a
link audit table. The bytes come from the stored copy of the package Document, through
`GET /api/batches/{id}/targets/{target_id}/files?path=<filename>`: `path` must equal one
of the target's `written_files[*].filename` exactly (anything else is a 404), text files
over 5 MB are refused for inline viewing (413) and `download=true` returns any file as an
attachment. There is no SharePoint fallback: a file whose stored copy is gone is a 404.

## Records and where they go

`POST /api/batches/{id}/finalize` (approver role) writes the batch's run-level
records once nothing is running, pending, packaging or awaiting a decision. A
finalized batch can be finalized again; the files are replaced. A prompt batch writes
three files:

| File | Contents |
|---|---|
| `manifest.json` | the batch, harness, template, columns, review type, counts and total cost |
| `results.jsonl` | one JSON object per row: `position, label, slug, status, attempts, cost_usd, model_used, iterations, run_id, error, outcome, task_input` |
| `results_summary.md` | a table `# / Label / Status / Cost / Answer` (answers cut to 160 characters, `|` and newlines escaped) and totals by status |

**With a write target** (`target_slug` and `write_root`, validated like the
disclosure profile's), the files are uploaded to `<write_root>/<run_slug>/` on the
SharePoint target, and each `finalize.files[*]` has a `web_url`.

**Without one**, each file is stored as a tret Document (`source_kind`
`batch_record`, `meta.batch_id`, `meta.record`) in the capture store under
`batches/<batch_id>/<n>/<name>` (`n` counts finalizes), and each `finalize.files[*]` has a `document_id` and
`byte_size`. Download one with `GET /api/batches/{id}/records/{name}`; the response
carries the Document's content type. Finalizing again writes new Document rows under a new
`n`, so older downloads keep their bytes; the batch points at the latest. If storing
fails part way, the previous file list stays and only `finalize.error` is set.

`GET /api/batches/{id}/records` previews the records at any time without writing
anything.

### Finalize and infrastructure failures

Finalize answers `409` if a target failed on an I/O or storage error (a full disk, a
read-only file system, the capture store): recording that as a finding about the row
would mislead. Fix the cause and retry the targets, or send `{"force": true}` to
finalize anyway.

## Guards

- **Free space.** The local capture store keeps every document and Document-backed
  record. `start` and `release` refuse with `409` when the free space on the volume
  holding `TRET_STORAGE_DIR` is below the number of targets still to run (pending or
  held) times `TRET_BATCHES_MIN_FREE_MB_PER_TARGET` (default 50). `0` turns the check
  off, and it is skipped on the `s3` storage backend (see [storage.md](storage.md)).
  `GET /api/settings/egress` reports `storage.free_mb` for the local backend.
- **Research allowlist.** For the disclosure profile, a known URL whose host the
  research allowlist would refuse (`TRET_EGRESS_RESEARCH_ALLOW_HOSTS`) is a warning
  at create. A prompt batch has no URL column to check; its harness's own egress
  limits apply to every run.
- **Templates.** Only plain `{column}` placeholders are accepted (whitespace inside the
  braces is ignored). Conversions (`{a!r}`), format specs (`{a:.2f}`), positional or
  empty fields and attribute/index access are a `422` that names the placeholder, and
  create trial-renders row 1. A row that still cannot render when launched fails that
  target alone.
- **Concurrency.** A batch runs at most `concurrency` (1-6) targets at once, and the
  whole process at most `TRET_COLLECTIONS_MAX_CONCURRENT`; a run is stopped after
  `TRET_COLLECTIONS_RUN_TIMEOUT_SECONDS`.

## API

Every route exists under `/api/batches` and, deprecated, `/api/collections`. Reads
need workspace membership; writes need the approver role.

| Route | Does |
|---|---|
| `POST /batches` | create (body discriminated on `profile`, default `disclosure`) |
| `GET /batches` | `{"batches": [...]}` (the alias returns `{"collections": [...]}`) |
| `GET /batches/{id}` | detail: settings, counts, `targets`, `pending_reviews` |
| `POST /batches/{id}/start`, `pause`, `resume`, `release` | lifecycle |
| `POST /batches/{id}/targets/{target_id}/retry` | retry a failed or rejected target |
| `POST /batches/{id}/targets/{target_id}/retry-write` | disclosure only: retry a failed upload |
| `GET /batches/{id}/records` | preview the records |
| `GET /batches/{id}/records/{name}` | a record kept as a Document (404 when the file went to SharePoint) |
| `GET /batches/{id}/targets/{target_id}/files?path=` | one of a target's written files (`download=true` for an attachment) |
| `POST /batches/{id}/finalize` | write the records; optional body `{"force": true}` |

The prompt batch's `settings` carry `harness_id, harness_name, template,
label_column, slug_column, review_schema, concurrency, first_batch_size,
target_slug, write_root, model_override, max_iterations, columns`.

### From the TypeScript SDK

The SDK's generated types cover these routes (`OpenApiComponents`, `OpenApiPaths`);
it has no dedicated batch methods yet, so call them with `fetch`:

```ts
import type { OpenApiComponents } from '@tret/sdk'

type CreatePromptBatch = OpenApiComponents['schemas']['CreatePromptBatchBody']

const headers = {
  Authorization: `Bearer ${token}`,
  'X-Tret-Workspace': workspaceId,
  'Content-Type': 'application/json',
}
const body: CreatePromptBatch = {
  profile: 'prompt',
  name: 'Supplier screening Q4',
  run_slug: 'supplier-screen-q4',
  harness_id: harnessId,
  template: 'Assess {company} ({country}).',
  csv: 'company,country\nAcme Ltd,DE\nGlobex,FR\n',
}
const batch = await (await fetch(`${baseUrl}/api/batches`, { method: 'POST', headers, body: JSON.stringify(body) })).json()
await fetch(`${baseUrl}/api/batches/${batch.id}/start`, { method: 'POST', headers })
// ...poll GET /api/batches/{id} until status is "completed" or "review", then:
await fetch(`${baseUrl}/api/batches/${batch.id}/finalize`, { method: 'POST', headers })
const results = await (await fetch(`${baseUrl}/api/batches/${batch.id}/records/results.jsonl`, { headers })).text()
```

## From a chat or harness

A chat turn can start a batch itself. Four builtin tools (`engine/batch_tools.py`)
ride along with `run_harness_task` on the seeded chat harness. They are added once per
workspace (`settings.batch_tools_seeded`), so an operator who removed delegation, or
removed `start_batch` itself, keeps them out; add them to another harness by name if you
want them there:

| Tool | Does |
|---|---|
| `start_batch` | creates and starts a prompt batch and returns at once: `{"batch_id", "name", "targets", "status", "estimated_cost_usd", "page": "/batches/<id>"}` |
| `batch_status` | counts by status, cost so far and the review queue: `{"batch_id", "name", "status", "counts", "cost_usd", "pending_reviews": [{"finding_id", "target_label", "schema_slug"}], "page"}` |
| `batch_results` | one row per target, paged (`offset`, `limit` up to 200; optional `status` filter): `{"total", "offset", "rows": [{"position", "label", "status", "answer", "finding", "cost_usd", "error"}]}`; `answer` is cut to 2000 characters |
| `batch_compare` | compares two batches of a family: `{"same_family", "quality_comparable", "quality_note", "shared", "summary", "rows" (the ten largest quality gaps), "page"}`; read-only (see "Families") |

`start_batch` takes `name`, `template` (`{column}` placeholders), the rows either inline
(`rows`, 1-500 objects) or as a CSV document of the workspace (`document_id`), and the
create options of the API (`label_column`, `review_schema`, `concurrency`,
`first_batch_size`, `model_override`, `max_iterations`). The harness that runs each row
defaults to the calling run's own harness (`harness_id` overrides it). The batch records
that run as its `parent_run_id`. Validation errors come back as the tool error with the
API's 422 text; a start refused by the free-space guard leaves a draft batch, which the
tool error names. The person whose turn calls `start_batch` needs the approver role in
the workspace, the same as creating a batch on the page; a CSV `document_id` must be a
`text/csv`, `text/plain` or `.csv` document of at most 2 MB; while the server is draining
for a deploy the tool asks for a retry in a minute.

**Disclosure collections.** `start_batch` also takes `profile: "disclosure"`, which starts
the Kith disclosure collection (the Disclosure Collector harness over a register of
companies, written to an M365 write target) instead of a prompt batch. Give the register as
`register_document_id` (a CSV document of the workspace, at most 2 MB) or `register_csv`
(inline text), and `target_slug` (required: the write target). `run_slug` defaults to
`<today>-<name>`, `write_root` to `runs`, `fiscal_year` to 2025, `auto_write` to true; a
taken `run_slug` comes back as the tool error and the model passes another.
`concurrency`, `first_batch_size`, `model_override` and `max_iterations` are shared; the
prompt-only parameters (`template`, `rows`, `document_id`, `label_column`, `review_schema`,
`harness_id`) and the disclosure-only ones are refused for the other profile. The result adds
`profile`, and, when the batch has warnings, the first five as `warnings` plus
`warnings_count` (the stored warning lines; the last may be a summary line such as "... and N more"). `estimated_cost_usd` is the Disclosure Collector's per-run cap ($8)
times the targets. Example:

```json
{"name": "Stage 1 (48)", "profile": "disclosure", "register_document_id": "<csv document id>",
 "target_slug": "disclosures", "first_batch_size": 12}
```

**Batch or delegation.** The tool descriptions give the model the rule: a batch is for
work that is large (tens to hundreds of rows), unattended, must be reviewable or
auditable later, or whose answers nobody needs in this turn. For a few independent items
whose results the turn needs now, `delegate_parallel` is still the tool. A batch's own
runs cannot start another batch, and subagents are never offered these tools.

**Cost.** Each time a target settles, the batch's total spend (every target, retries
included) is compared with what it has already reported (`settings.rolled_up_cost_usd`)
and the difference is added to the parent run's `delegated_cost_usd`, in one commit under
the launch lock. It is idempotent, so a re-settle changes nothing, and it never fails a
settle. While the parent run is still running the roll-up waits, so a batch cannot trip
the turn's own cost cap; it catches up on the next settle (or recompute) once the parent
has finished.

In the chat, a successful `start_batch` shows a card under the reply (name, profile, target
count, status, cost, register warnings if any, a link to the batch page) that refreshes while the batch is running.

## From the command line

`tret batch` is an HTTP client of a running server (the batch executes inside the
server, so there is no database mode).

```
tret batch [--url URL] [--token T | --email E --password P] [--workspace ID] [--json] <command>
```

| Option | Meaning |
|---|---|
| `--url`, `TRET_URL` | server base URL, default `http://localhost:8000` |
| `--token`, `TRET_TOKEN` | bearer access token (the server needs `TRET_OIDC_API_AUDIENCE`) |
| `--email`/`--password`, `TRET_EMAIL`/`TRET_PASSWORD` | log in once at `/api/auth/login`; the session cookie is kept for the command. Prefer the environment variables: an argument is visible in `ps`. Use an https URL when the server marks its cookie secure |
| `--workspace` | workspace id, sent as `X-Tret-Workspace` (and switched with `POST /api/auth/workspace` after a password login) |
| `--json` | print the raw JSON; with `status --watch`, one JSON object per poll |

The options may come before or after the command. Commands: `create` (both profiles),
`start ID`, `status ID [--watch] [--targets] [--interval S]`, `release ID`,
`approve ID (--all | --finding ID ...) [--reject] [--note TEXT]`, `finalize ID [--force]`,
`list`. Exit codes: 0 ok, 1 the server refused (its detail is printed) or was unreachable
(or a watched batch ended `failed`), 2 usage or configuration. Neither the token nor the
password is ever printed.

```sh
export TRET_URL=https://tret.example.org TRET_TOKEN=...   # or TRET_EMAIL / TRET_PASSWORD
tret batch create --name "Supplier screening Q4" --slug supplier-screen-q4 \
    --harness 6d1c0c1e-... --template @prompt.txt --csv suppliers.csv \
    --review any --gate 5 --concurrency 4
tret batch start <id>
tret batch status <id> --watch          # polls every 10 s until completed/finalized/failed/paused, or gate/review (waiting for a person)
tret batch release <id>                 # after checking the first 5 targets
tret batch approve <id> --all           # when review is on and findings are waiting
tret batch approve <id> --all --packaging-only   # approve page maps only to package them (not a quality review)
tret batch finalize <id>
```

A disclosure batch is `tret batch create --profile disclosure --name ... --register
register.csv --target disclosures [--write-root runs] [--fiscal-year 2025]
[--no-auto-write]`.

## Scorecards, comparison and the evaluator

Every batch has a **scorecard** (`GET /api/batches/{id}/scorecard`), computed from rows
it already has; no model is called. Every target falls into one class: `success`
(written or completed), `gap`, `rejected`, `failed`, `infra`, `timeout` or `unsettled`.
A failed or gap target whose error or outcome summary reads as a storage or I/O fault
("insufficient storage space", `ENOSPC`, a read-only file system) is `infra`, and one
that hit the run time limit is `timeout`: both count against **reliability**, never
against the model, and leave the yield denominator. The scorecard reports:

- **yield**: acceptable results / (targets - infra - timeout - unsettled). Without an
  evaluation a result is acceptable when it is a `success`; with a scored evaluation
  it is acceptable when its quality reaches the rubric's `acceptable_quality` (60),
  so an honest gap can be acceptable. A target with no fresh score falls back to its
  class. `written_share` is the same ratio over successes only.
- **cost and Wh per acceptable result** (target spend only; energy is null when no run
  recorded it, and a retried target's earlier attempts' energy is not counted), **wall time** (batch start to the last target finishing), **retries**,
  human review counts, the model mix and **outliers**: robust z-scores (0.6745 x
  (x - median) / MAD) for cost, iteration ratio, retained pages and retained/source
  page ratio, flagged at |z| >= 3.5 when at least five targets and a non-zero MAD.

`GET /api/batches/compare?a=<id>&b=<id>` puts two scorecards side by side and matches
rows by **row key** (the target slug for a disclosure batch; for a prompt batch the
slug column, else the label as ascii letters, digits and hyphens). Only rows present in
both batches appear in `rows`; `only_a` / `only_b` count the rest. Quality is compared
only when both batches are the same **family** (`disclosure:fy<year>`, or the same
harness and template for a prompt batch), both have an evaluation and the two used the
same rubric version and judge model; otherwise `quality_note` says why. Wins, ties
(under 5 points) and the mean quality delta come with a 90% bootstrap interval (2,000
resamples, fixed seed, at least three pairs). Cost and energy are summed over shared
rows only.

Failed and gap rows score near zero or on gap honesty alone, so a model with a low yield
can look worse even when the packages it delivered are as good. `summary.delivered`
therefore repeats the comparison over the **both-delivered** rows only (each row has
`both_delivered`: both sides `success` with a score): `n`, wins, ties, both mean
qualities, the mean delta and its interval; all but `n` are null when quality is not
comparable. A row where one side is a `gap` and the other side delivered an acceptable
result (scored at or above the bar, no disqualifying flag such as `wrong_year` or
`missed_primary`) in batches of the same family is a **disputed gap**: the row's `disputed_gap` is `"a"` or `"b"` (the
side that gave up) and `summary.disputed_gaps_a` / `disputed_gaps_b` count them.

### Evaluating a batch

Pass `evaluation: {"enabled": true, "judge_model": null, "budget_usd": null, "rubric": null}`
when creating a batch (or `evaluate: true` to `start_batch`), or `POST /api/batches/{id}/evaluate`
at any time, including after the batch is finalized. A judge model scores each target as
it settles, using the seeded **Batch Evaluator** harness (workspace-wide, read-only,
tools `read_document`, `search_documents`, `submit_evaluation`, `submit_batch_review`).

- **Judge and budget.** One judge per pass. `TRET_BATCH_EVALUATOR_JUDGE` is an ordered,
  comma-separated preference list (default `anthropic/claude-opus-5-5,openrouter/google/gemini-3.8-flash`):
  a pass uses the first entry that is in the catalog and whose provider has a key. A
  Claude judge needs an Anthropic key; with only an OpenRouter key (Tret Cloud) the pass
  falls back to the Gemini Flash entry, a different model family from the usual OpenAI
  collection runs, at a low price. A judge named in the request or on the batch must be
  usable: an unknown model or one whose provider has no key is a 422, and so is a list
  with nothing usable. The batch stores a judge only if its creator named one; the
  pass records the resolved judge, and Re-evaluate resolves again. If the auto pass of
  a starting batch finds no judge, the batch still starts and the pass is `failed`
  with the reason. The budget defaults to 1.5 x
  the estimate (25,000 input and 1,500 output tokens per target at the judge's catalog
  price), at least $1. When the next judge run would exceed it, the remaining targets
  become `not_evaluated_budget`. Evaluation spend is the pass's (`evaluation_cost_usd`),
  never the targets', and is added to the parent run's `delegated_cost_usd` with them.
- **Rubric.** A disclosure batch is always judged on `disclosure-v1` (right document,
  coverage, page-map precision, provenance, gap honesty); a prompt batch on
  `prompt-default-v1` (correctness, completeness, instruction following, grounding) or
  its own validated rubric (2-8 criteria, weights 1-100, anchors for a 5 and a 1, up to
  15 flags). `GET /api/batches/rubrics/default?profile=` returns the built-in ones. The
  rubric is frozen onto the batch and onto each pass. Quality is the weighted mean of
  (score - 1) / 4 x 100 over the criteria that apply to the target and have a score.
- **Blind evidence pack.** The judge sees the rubric, the task, the result, the page
  maps and excerpts (disclosure) and a trail of tool calls, with the run's documents
  attached; never the model, provider, spend, tokens, energy, iteration count or routing.
  Infrastructure failures and timeouts are `skipped`, not judged.
- **Cross-check of a gap.** A judge that sees a gap alone can score a well-argued reason
  100 even when another batch of the same target list delivered that company's
  disclosure. So for a gap target of a disclosure batch the pack gains a section, "Cross-check:
  other runs of this target list": up to three acceptable deliveries of the same company
  in other batches of the family (current pass, not stale, at or above the bar, no
  disqualifying flag) with date, quality, the outcome's status and reporting period and the source URLs,
  never batch names or models. The judge tests the gap's reason against it: a document on
  the issuer's own site that the stated reason does not explain scores gap honesty at
  most 2 with `gap_reason_wrong`; a gap that only an archive or third-party copy could
  fill can still score well. Nothing is overridden in code. The row records what it was
  checked against (`relative.gap_check`, kept when ranks are recomputed), the scorecard's
  `quality` gains `gaps_disputed` and `gaps_rechecked`, and each target's `evaluation`
  gains `delivered_elsewhere` and `gap_rechecked`. "Only unscored" keeps a scored gap
  only while the deliveries elsewhere are exactly the ones its judging saw; a new one, or
  a shown one that stopped qualifying, makes it pending again (unselected rows of a
  targeted re-judge are kept as they were).
- **Cross-check of a delivered row (main documents).** The mirror image: a run that
  packaged only companion documents (a frameworks index, a scorecard, a glossary) can be
  scored like a run that packaged the 100-page report, when a register note says no main
  PDF exists. So for a delivered target of a disclosure batch the pack gains "Cross-check:
  main documents other runs delivered": up to three documents (role `primary_disclosure`
  or `embedded_statement_and_assurance`) that acceptable deliveries of the same company in
  other batches of the family packaged and this run did not (matched by sha256 or by
  normalised URL, any role of this run's own packages counts; the scheme, a leading
  `www.`, default ports, the fragment and a trailing slash do not make a different URL).
  A main-role source under 25% of the pages of this run's largest main-role source and
  under 20 pages is not counted (an 11-page index labelled primary does not dispute a
  100-page report, but an 80-page report still disputes a 400-page filing; unknown page
  counts are not guarded). Each is shown with the URL, page count,
  how many other runs packaged it and the best quality, never batch names or models. The
  judge cannot open them: it tests whether this run captured the primary disclosure (at
  most 2 on right_document with `missed_primary` when only companions were packaged
  although the report was available; no fault when this run holds the same disclosure in
  another form, another year or a thematic report). The pack's task block now calls the
  register notes "may be out of date; the documents decide". Code never changes a score.
  The row records the document keys it was shown (`relative.primary_check`, kept when
  ranks are recomputed, one list of document keys per shown document); "Only unscored"
  re-judges a scored delivered row when a new document appears or a shown one stops
  qualifying, not when another run delivers a document already shown (a document is the
  same while its sha256 or normalised URL overlaps, so a re-fetched copy with new bytes
  or a URL-only document that gains a hash is not new). The scorecard's `quality` gains
  `primaries_disputed` / `primaries_rechecked` (counts of delivered rows, not documents), each target's `evaluation` gains
  `primary_elsewhere` / `primary_rechecked`, and the comparison's rows carry
  `disputed_primary` (`a`, `b` or `both`, same family only) with
  `summary.disputed_primaries_a` / `_b`.
- **Relative scores.** Each scored row gets a rank, a percentile and an expected score:
  the same row's history in other batches of the family (shrunk toward the batch mean),
  else the batch mean moved 5 points per difficulty step. `residual` = quality - expected.
- **Batch review.** When every target is judged, one more run reviews the whole batch
  (inconsistent scoring, patterns, up to five lessons). A release or a retry later
  re-opens the pass and replaces the review at the end.
- **Spot checks.** An approver can mark a scored row agree, disagree or adjust
  (`POST /api/batches/{id}/targets/{target_id}/evaluation/review`). The pass reports
  `spot_checks` and `agreement` (agree, or adjust within 15 points).
- **Records.** With a scored pass, finalize adds an `evaluation` block to each result
  (or company) and to the manifest, and a Score column to the summary; with none,
  the records are exactly as before.
- **Environment.** `TRET_BATCH_EVALUATOR_JUDGE`, `TRET_BATCH_EVALUATOR_CONCURRENCY`
  (evaluator runs at once, 1-4, default 2) and `TRET_BATCH_EVALUATOR_RUN_TIMEOUT_SECONDS`
  (default 300). `POST .../evaluation/cancel` stops a running pass.

**Review lessons.** Each of the batch review's "Lessons for future runs" gets exactly one
result, shown on the evaluation card: `proposed` (a pending lesson on the harness), `duplicate`
(it matches an existing lesson, which the entry points at, whatever its status), `rejected_text`
(not plain language, or empty once invisible characters are removed), `dropped` (only the first
five are proposed per batch) or `unresolved` (no harness could be found). Proposals go to the
harness the batch's targets actually ran on (the most common one), not the evaluator's. A later
pass replaces the batch's still-pending proposals; approved, rejected and retired lessons are never
re-proposed verbatim. Invisible characters (zero-width, bidi overrides, control, tag and variation-selector
characters) are stripped from lesson text (a joiner is kept only between two letters or symbols,
and a variation selector only right after an emoji or other symbol), so a reviewer sees exactly what would be injected. Nothing reaches a
prompt until an approver approves a proposal, on the card or in the harness's Lessons section.
Approved lessons are added to every future run of that harness (not to evaluator runs) and never
change scores.

### The pairwise judge

Rubric scores grade each target alone, so two batches that both score 80 on a row look
tied even when one holds far more of the required disclosure. The **pairwise judge**
(`tret/batches/pairwise.py`) puts the same row's output from two batches of a family in
front of one judge, side by side, and asks which is the better delivery for that task
under the rubric. Use it on the rows the rubric cannot separate (`near_ties`: both
delivered, scores within 5 points) or on every row both delivered (`both_delivered`).

- **Blind and order-swapped.** The message holds the rubric and the task once, then
  "Output 1" and "Output 2". Each output is scrubbed of its own run's model and provider
  names and carries no cost, token or iteration count, batch name or "which batch is
  this". Every pair is judged twice, once in each order. Both calls prefer the same side:
  a **clear** win. A side and a tie: a **lean**. Both ties: a tie. Opposite sides:
  **inconsistent** (position bias, treat as no verdict).
- **It never changes a score.** Verdicts are shown beside the rubric numbers (a "Pairwise"
  column and a tally on the compare page); `quality`, yield and the scorecard are
  untouched, and the rubric is unchanged. Judge runs carry `_pairwise_judgment_id`, so the
  router learns nothing from them (`run_outcomes` rows are not written).
- **Stored once per pair.** A judgment is keyed by the two targets (canonical order),
  the judge and the rubric version, so judging A against B and later B against A reuses it.
  It goes stale when either target is retried or re-run and is judged again on the next
  start. Only batches that are quality-comparable (same family, rubric and judge) can be
  judged.
- **Long tasks are cut.** The task shown to the pairwise judge is cut at 12,000 characters
  (and the rubric at 8,000), marked `[task cut at the size limit]`, so on very long prompt
  inputs the judge compares the outputs against a truncated task.
- **One pass at a time, and a stable rubric.** Starting a second pass for a pair that is
  already pending or running (including two concurrent starts) is a 409. A judgment is
  tied to the rubric version it started with: if the batches' current rubric no longer has
  that version the judgment fails with "the rubric changed; start the pairwise judge again".
  Existing workspaces' Batch Evaluator doctrine is upgraded to mention `submit_pairwise`
  only when it still equals the text an earlier release seeded; an edited doctrine is never
  overwritten (the pairwise message itself says it is not a scoring run).
- **Cost.** Two judge calls per pair, about the cost of two per-target evaluations
  (roughly $0.10 to $0.25 on an Opus-class judge); the default budget is 1.5 x pairs x 2 x
  that estimate (floor $1). When the pass's spend reaches the budget the remaining pairs
  are `skipped` ("budget"). A judge that ends without calling `submit_pairwise` fails its
  pair.
- **API.** `POST /api/batches/compare/pairwise` (approver; `a`, `b`, `scope`, `max_pairs`
  1-100, `budget_usd`) starts it and returns the status; `GET
  /api/batches/compare/pairwise?a=&b=` returns the status with one row per pair (verdict
  `a` | `b` | `tie` | `inconsistent`, strength, the two calls' rationales);
  `POST .../compare/pairwise/cancel` stops it. `GET /api/batches/compare` adds `pairwise` to
  each row and `summary.pairwise` (zeros until something is judged).

### Families

`GET /api/batches/families` lists the workspace's batch families (disclosure by fiscal
year; prompt batches by harness and template) and `GET /api/batches/family?key=<family_key>`
returns the **leaderboard**: one row per batch (yield, quality, cost and Wh per acceptable
result, models, judge, whether it shares the family's comparable set) and one row per
model. The comparable set is the most common rubric + judge among the batches' current
passes (ties go to the newest); other batches are counted in `excluded_batches` and
left out of the model table. Per model: delivered, yield, mean quality with a 90%
bootstrap interval (three or more scores), mean residual, cost and Wh per acceptable
result, p95 iterations and cost over acceptable runs, infrastructure failures and a Pareto
mark (not beaten on both quality and cost per acceptable result, among models with three
or more scored rows). The page is `/batches/family?key=...`; it is computed on demand and
never polled.

The chat harness gains `batch_compare` (batch_a, batch_b): the compare summary, the
pairwise tally and the ten rows with the largest quality difference, read-only. It is
offered once to existing chat harnesses that still list the other batch tools
(`settings.batch_compare_seeded`).

### Advice from earlier batches

`POST /api/batches/advice` (member) says what earlier batches of the same family suggest
for a batch you are about to create. Body: `{profile: "disclosure" | "prompt", fiscal_year?,
harness_id?, template?}`; a prompt request needs `harness_id` and `template`, and the
harness must exist in the workspace and not be archived (422 otherwise). It is read-only:
nothing is applied unless a person, or the calling agent, picks it. The create form shows it and has an Apply button per suggestion; the
`start_batch` tool returns the same suggestion, computed before the new batch is counted,
as `advice` (null when there is no evidence or computing it failed).

The rule, in plain words. The evidence is the family's per-model aggregate over its
comparable batches (see Families). A model is eligible with at least 5 scored rows and a
quality interval; its lower bound is the low end of that 90% interval. Under the
`quality` objective the eligible model with the best lower bound is recommended. Under
any other objective, the cheapest model per acceptable result among those whose lower
bound is within 3 points of the best lower bound (5 points under `eco`). The objective is
the harness policy's (default `balanced`). Then:

- `max_iterations` = 1.25 x the model's 95th-percentile iterations on acceptable runs,
  rounded up and capped at 50 (null if unknown);
- `cost_cap_usd` = 1.5 x its 95th-percentile cost on acceptable runs, rounded up to the
  cent and never below $0.01 (null when that p95 is 0: a free or unreported-cost model);
- `concurrency` = one lower than the family's most recent batch when more than 10% of its
  targets ended as infrastructure failures or timeouts, else null.

The reason sentences carry the numbers, for example "cheapest model whose quality lower
bound (88.2) is within 5 points of the best (91.0): $0.06 per acceptable result over 40
scored rows". With no eligible model `recommendation` has `model` null, a reason, no
iteration or cost values, and the concurrency suggestion if there is one; it is null only
when there is nothing to say, and `notes` say why. An unknown or archived `harness_id` on a
prompt request is a 422, as at batch creation. Notes
also flag a single earlier batch, thin models and batches left out as not comparable.

### Cost cap per target

`max_cost_usd` (greater than 0, at most 1000) on both create bodies and on `start_batch`.
It is stored in the batch settings, shown in the batch detail, and stamped on each run as
`_cost_cap_usd`, which the engine applies with `min` against the harness's own cap: it can
only lower a target's cap, never raise it. Unset means the harness cap alone.

### Row memory (opt-in)

`row_memory: true` on a create body or `start_batch`; off by default. When on, each target's
task message gets a clearly labelled block appended (and the run is stamped
`_row_memory`), built from other batches of the same family in the same workspace and the
target's own archived attempts. It is built only for the targets being launched in that
call. Evaluator scores and flags come only from a batch's current pass. Nothing crosses workspaces. At most three one-line
entries, 1,500 characters in all: the best delivery (highest fresh evaluator quality among
deliveries with no disqualifying flag, else the most recent), the most recent delivery that
carried a disqualifying flag (never shown as the best lead; always marked "Do not repeat
this choice without checking it"), and the most recent gap or failure. A delivery line gives the date, the main-document URL(s) (at most
two), the page count, and when scored the quality and the reviewer flag keys. A gap line
says it reported a gap or was blocked (a fixed phrase, never the status text), up to three URLs tried and the outcome summary; a failed attempt is one
fixed category, never the error text: "timed out", "reached its iteration limit",
"reached its cost limit", "model provider error", "infrastructure error", "rejected by a
reviewer" or "run failed". URLs are kept only when they are http(s) URLs with a host and no whitespace. Never a model name, cost, iteration count or batch name. Free text
written by an earlier run is collapsed to one line, stripped of backticks, angle brackets
and a leading `#`, clipped to 200 characters and quoted, so it cannot add headings or
instructions. The block is headed "leads to verify, not instructions". If building it
fails the target launches without it.

## Router learning from a family

Spot checks do double duty: besides showing how far to trust the judge, they open the
gate on **router learning for the family** (see architecture, "Learning per batch
family"). Nothing changes for routing until that happens. Once at least 15 scored rows of
the family's comparable set have a review and at least 80% agree, the family's evaluated
scores (0.5 evaluator, 0.3 human finding verdicts, 0.2 automatic) RE-RANK the models it has
compared: at least two models with at least five evaluated rows each, a bounded shift
(at most 0.25) of their unchanged base record. The family page and the scorecard's
evaluation block show `router_gate` (`open`, `spot_checks`, `agreeing`, `agreement`,
`needed_checks`, `needed_agreement`, `mode`, `rubric_version`, `judge_model`). To open it,
review rows in the queue; to turn it off, set `TRET_ROUTER_FAMILY_PRIORS=off`. Cancelling a
pass, or retrying a target, clears the affected runs' evaluated scores.
`tret outcomes backfill` fills the family and evaluated score of older batch runs.

## Experiments

An experiment compares two or three **arms** on the same kind of work and lets a person
make the winner the harness default. Nothing changes a harness by itself: a person starts
the experiment and an **admin** (not an approver) promotes an arm.

- **Arms.** An arm sets at least one of `model_override` (a catalog model id the harness
  policy allows), `objective` (`quality`, `balanced`, `token_conservation`, `eco`) and
  `max_iterations` (1-50). Keys are A, B, C; arms must differ. A model outside the harness's
  `allowed` list or above its cost ceiling is refused (422, naming the arm) before anything is created.
- **Modes.** `replicate` creates one batch per arm over the same rows (run slugs `<slug>-a`,
  `-b`, `-c`; `sample_n` keeps that many rows, spread evenly across market and category,
  the same rows in every arm). The comparison is paired. `split` creates one batch and gives
  each row to one arm, round-robin within market and category in register order starting
  with A. The comparison is unpaired.
- **Create and start.** `POST /api/batches/experiments` (approver) with `{name, mode, arms,
  sample_n, batch}` where `batch` is a normal create body; creation is all-or-nothing.
  `POST /api/batches/experiments/{id}/start` starts every arm. `start_batch` accepts
  `experiment: {mode, arms, sample_n}` and starts it for you. Reads: `GET
  /api/batches/experiments` and `/{id}`.
- **Reading the result.** Per arm: settled rows, delivered, yield, quality mean with its 90%
  interval, residual, cost and energy per acceptable result, iterations. Comparisons are
  against arm A. Paired comparisons use the batch comparison (wins, losses, ties, mean
  difference with a 90% interval over shared rows). Split comparisons are marked "different
  rows" and show a difference of means with a 90% bootstrap interval; a difference whose
  interval includes zero is not evidence. The recommendation applies the advice rule (cheapest
  per acceptable result within 3 quality points of the best lower bound, 5 for `eco`, best for
  `quality`; at least 5 scored rows) and is empty while rows are unsettled or unscored.
- **Make default.** `POST /api/batches/experiments/{id}/promote {arm}` (admin) executes the
  server's promotion plan, which every arm also shows as `promotion` in the result
  (`allowed`, `blocked_reason`, `changes`, `notes` and a `token`). `changes` lists everything that
  would differ on the harness (Model, Objective, Max iterations, and any other policy or loop key
  as `policy.<key>` / `loop.<key>`), not only what the arm sets; the request may carry the
  `token` it was shown, and a stale one is a 409 so nothing is written under a changed harness. Make default applies
  only an arm's objective (the policy `objective`) and iteration limit (`loop_config.max_iterations`);
  an arm's model is never written to a harness, and `mode`, `model`, `allowed` and every other
  policy key stay as they are. A model arm is evidence for the router: its evaluated runs feed
  the per-family evidence once the family's spot-check gate is open, and an arm that sets only a
  model is not promotable (`allowed` false, 409 on `promote`). Creation still refuses a model the
  policy does not allow. The plan is refused (409) if the harness already has these settings.
  While the harness still has an earlier promotion of the same experiment, a
  second arm replaces it: it is applied on top of the policy from before the experiment's
  first promotion. `experiment.current_arm` names the arm whose objective and iteration limit the harness has now
  (the promoted arm when several arms would change nothing, never a model-only arm).
  The experiment page has a "What the router would pick now" check that asks the router for this
  agent and family on request. Each check is one small router call on the workspace's own key; it
  does not explore, and it sizes the task from the agent's instructions alone, so a real run can
  differ (`POST /api/routing/preview` with `family_key`; the server adds the caller's workspace,
  and the response carries `family_priors` only when family evidence was applied). The result
  carries `experiment.router_gate`, the family's spot-check gate. An experiment never sets a
  pin; manual pinning in the harness editor is a separate operator override. When a promotion
  replaces an earlier one, the policy returns to its pre-experiment baseline, whatever that was.
  The change is validated like a harness edit and recorded in the harness's policy history
  with who, when and the previous values; two concurrent promotions serialise on the harness row.
- **Reading while it runs.** Intervals stay empty until every row of every arm has settled,
  and the recommendation stays empty until then. `experiment.evaluating` is true while a
  scoring pass is running; `experiment.warnings` lists setup cautions (an objective-only arm
  on a pinned or overridden model; replicate arms with write-back each writing their own
  copy of the packages). Row memory never shows an arm the attempts of its sibling arms.
- **Split balance.** Arms are dealt in register order, rotating across market/category
  groups, so arm counts differ by at most one overall. A split needs at least one row per
  arm, and `sample_n` must be smaller than the row count.
- **History and revert.** `GET /api/harnesses/{id}/policy-history` lists every change (edit,
  experiment, revert); `POST /api/harnesses/{id}/policy-history/{history_id}/revert` (admin)
  restores that row's previous values and records a revert.

## Agentic test probes

For the agentic test protocol (id, level, identity, steps, oracle, expected cost). All
run on a dev or staging workspace with the chat harness and a small harness with the web
tools off as the batch harness.

**BATCH-01. start_batch from chat.** Level: L2 (chat to engine to batch). Identity:
approver. Steps: in a fresh chat say "Run this on each row: assess {company}'s climate
disclosure in one paragraph" and attach or paste a 5-row CSV. Oracle: the model calls
`start_batch` (not `delegate_parallel`); the result has `batch_id` and `targets` 5;
`GET /api/batches/<id>` shows `profile` prompt and `parent_run_id` equal to the chat
turn's run id; a card with the batch name and a working link appears under the reply and
survives a page reload. Expected cost: about $0.05-0.20.

**BATCH-02. The model chooses between batch and delegation.** Level: L3. Identity:
approver. Steps: (a) "Compare these 3 sites' flood exposure now, I need the answers in
this reply" with 3 short items; (b) in a new chat, "Screen these 40 suppliers, I will look
tomorrow" with 40 rows. Oracle: (a) calls `delegate_parallel` and no `start_batch`; (b)
calls `start_batch` and no `delegate_parallel`. Repeat each three times; at least 5 of 6
correct. Expected cost: about $0.60 including the 40-row batch on an economy model.

**BATCH-03. Status and results after completion.** Level: L2. Identity: approver. Steps:
after BATCH-01 completes, ask "how did that batch go?" then "summarise the answers".
Oracle: the model calls `batch_status` then `batch_results`; counts match the Batches
page; every claim in the summary cites a row label that exists in the batch; a failed
target, if any, is reported as failed rather than summarised. Expected cost: under $0.05.

**BATCH-04. Review on, bulk approve.** Level: L3. Identity: approver. Steps: start a batch
with `review_schema` "any" over a harness that proposes a finding for each row (or a
`connected_write` request); open the batch page. Oracle: targets stop at `review`
and `pending_reviews` lists them (`batch_status` agrees); bulk approve from the Batches
page (or `tret batch approve <id> --all`) moves them on; the batch reaches `completed`.
Expected cost: about $0.10.

**BATCH-05. Finalize without a write target.** Level: L2. Identity: approver. Steps:
finalize a completed batch that has no `target_slug`. Oracle: `finalize.files` lists
`manifest.json`, `results.jsonl`, `results_summary.md`, each with a `document_id`; each
downloads from `GET /api/batches/<id>/records/<name>`; `results.jsonl` has one line per
target. Expected cost: none.

**BATCH-06. Parent cost to the cent.** Level: L3. Identity: approver and (Tret Cloud) an
admin. Steps: after BATCH-01 completes, compare the parent run's `delegated_cost_usd` with
the batch's `cost_usd`; retry one failed target if there is one and compare again. Oracle:
the two agree to the cent after each settle (a retry adds only its new spend); on Tret
Cloud the ledger shows one charge per batch run and none for the roll-up itself. Expected
cost: none beyond the batch.

**BATCH-07. Guards.** Level: L2. Identity: approver. Steps: on a local-storage dev server
set `TRET_BATCHES_MIN_FREE_MB_PER_TARGET` above the free space and start a batch (from
chat and with `tret batch start`); separately, with research egress on and an allowlist,
create a batch whose CSV carries a URL host outside it. Oracle: the start is refused with
the "Not enough free space on the capture store" text (the chat tool error says the batch
was created but not started, and the draft is on the Batches page); the create response
carries the allowlist warning. Expected cost: none.

**BATCH-08. CLI end to end.** Level: L2. Identity: approver with a token or password.
Steps: `tret batch create`, `start`, `status --watch`, `approve --all` if review is on,
`finalize`, `list` against a dev server, with `--json` on one of them. Oracle: exit code 0
for each; the watch returns when the batch is completed; the records exist as in
BATCH-05; a wrong password prints `login failed: 401` and exits 1 without echoing the
password; a missing `--harness` exits 2. Expected cost: about $0.05.

**BATCH-EVAL-01. Scorecard and compare.** Level: L2. Identity: member. Steps: run two
disclosure batches over registers that share companies (same fiscal year), then open
`GET /api/batches/<a>/scorecard` and `GET /api/batches/compare?a=<a>&b=<b>`. Oracle: the
class counts sum to the target count; yield, cost and energy per acceptable result match
the targets by hand; `shared` equals the number of common slugs; without evaluations
`quality_comparable` is false with a note. Expected cost: the two batches only.

**BATCH-EVAL-02. Evaluation on create.** Level: L2. Identity: approver. Steps: create a
5-row prompt batch with `evaluation: {"enabled": true}` and start it. Oracle: all five rows
reach `scored` with a quality, flags, rank and residual; the pass completes with a batch
review (narrative and lessons); the batch's `cost_usd` excludes `evaluation_cost_usd`;
the parent run's `delegated_cost_usd` includes both. Expected cost: about $0.50.

**BATCH-EVAL-03. Retroactive evaluation.** Level: L2. Identity: approver. Steps: finalize
a disclosure batch, `POST /evaluate`, then `POST /evaluate` again with `only_missing` true.
Oracle: the first pass scores every settled non-infra target; infra targets are skipped;
the second pass re-judges nothing (same scores, no new evaluator runs); finalize again
adds the evaluation block to the records. Expected cost: about $0.10 per target.

**BATCH-EVAL-04. A wrong row is flagged.** Level: L3. Identity: approver. Steps: in a
disclosure batch, include a company whose run collected the wrong fiscal year (or retry a
target with a wrong known URL). Oracle: that row carries `wrong_year` (or `wrong_entity`),
a low `right_document` score, the lowest rank and a negative residual. Expected cost:
about $0.10 per target.

**BATCH-EVAL-05. Budget stop.** Level: L2. Identity: approver. Steps: evaluate a batch with
`budget_usd` set to about 1.5 x one judge run. Oracle: one or two rows are `scored`, the
rest `not_evaluated_budget`; the pass cost stays within one judge run of the budget and
the pass completes. Expected cost: about $0.30.

**BATCH-EVAL-06. Spot checks.** Level: L2. Identity: approver. Steps: after
BATCH-EVAL-02, mark one row agree, one adjust within 15 points of its score and one
disagree. Oracle: `spot_checks` is 3 and `agreement` is 2/3; an analyst gets 403; a
non-scored row is a 409. Expected cost: none.

**BATCH-EVAL-07. Pairwise judge on near ties.** Level: L2. Identity: approver. Steps: with two
evaluated batches of one family and one judge, open the compare page, run the pairwise
judge on `near_ties` with 4 pairs. Oracle: each pair gets a verdict `a` / `b` / `tie` /
`inconsistent`, the rubric qualities on the page are unchanged, the status shows 8 judge
calls of cost, and `run_outcomes` has no new rows. Expected cost: about $0.60.

**BATCH-EVAL-08. Pairwise is blind and order-stable.** Level: L2. Identity: approver. Steps:
run BATCH-EVAL-07, then read the judge runs' task messages. Oracle: no model, provider,
batch name, dollar amount or token count appears; the same row appears in both orders;
starting the judge again (A, B or B, A) judges nothing new. Expected cost: none beyond
BATCH-EVAL-07.

**BATCH-EVAL-09. Pairwise stale and budget.** Level: L2. Identity: approver. Steps: retry one
judged target and re-score it, then start the pairwise judge again with `budget_usd` set
to about one pair. Oracle: only the retried row is judged again, the others stay; with a
tiny budget the first pair finishes and the rest are `skipped` with error `budget`.
Expected cost: about $0.30.

**BATCH-EVAL-10. Family leaderboard.** Level: L2. Identity: member. Steps: open
`/batches/family?key=disclosure:fy2025` with three or more evaluated batches, one scored by
a different judge. Oracle: that batch is marked not comparable and counted in
`excluded_batches`; the model table lists each model once with yield, quality interval and
a Pareto mark; clicking a column header re-sorts; picking two batches opens the compare
page. Expected cost: none.

**BATCH-EVAL-11. `batch_compare` from chat.** Level: L2. Identity: approver. Steps: in the
chat, ask to compare two batches by id. Oracle: the reply cites `wins`, the quality delta
and its interval, the pairwise tally and links the compare page; asking about a batch of
another workspace is refused. Expected cost: about $0.05.

**BATCH-EVAL-E3-01. Advice.** Level: L1. Identity: member. Steps: in a workspace with two
evaluated disclosure batches of one fiscal year, `POST /api/batches/advice` with
`{"profile": "disclosure", "fiscal_year": <year>}`; repeat with another fiscal year.
Oracle: the first returns per-model evidence and a recommendation whose model has at least
5 scored rows and whose reason quotes its lower bound and cost; `max_iterations` is at most
50; the second returns `recommendation: null` and a note. Expected cost: none.

**BATCH-EVAL-E3-02. Row memory.** Level: L2. Identity: approver. Steps: create a batch of a
register already collected once with `row_memory: true`, start it and open one run's task.
Oracle: the task message ends with the EARLIER ATTEMPTS block, at most three lines, with no
model, cost or batch name; the same batch with `row_memory` off has no block. Expected
cost: the batch only.

**BATCH-EVAL-E3-03. Cost cap.** Level: L2. Identity: approver. Steps: create a batch with
`max_cost_usd: 0.05` over a harness whose own cap is higher. Oracle: a run that reaches the cap stops
(the engine's cost-cap stop); a cap above the harness's own changes nothing. Expected
cost: under $0.50.

**BATCH-EVAL-EXP. Experiment, read, promote, revert.** Level: L3. Identity: approver, then
admin. Steps: from a chat ask for a 12-row batch "as an experiment: replicate, arm A the
default model, arm B an economy model, sample 6"; open the page; after it settles read the
comparison; as an admin press Make default on one arm, then revert from the harness's policy
history. Oracle: two arm batches over the same 6 rows; the page shows n and the interval beside
every mean; an approver gets 403 on Make default; promotion adds one history row naming the
admin; revert restores the previous policy and adds a "revert" row. Expected cost: about $0.30.
