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
