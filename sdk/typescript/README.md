# @tret/sdk

A typed TypeScript client for the [tret](../../README.md) API: start runs,
stream their events, upload documents, review findings, and get back the
same honest cost/carbon **Receipt** the Python SDK prints.

- Zero runtime dependencies. Built on `fetch`, web streams, `FormData` and
  `AbortController`, so the same package runs in Node ≥ 18, Electron (main or
  renderer) and the browser.
- ESM and CommonJS builds, with `.d.ts` types.
- Its own Server-Sent Events parser over `fetch`, not `EventSource`, so it can
  send `Authorization` and `X-Tret-Workspace` headers and reconnect on its own.

## Install

`@tret/sdk` is not on npm yet. Until it is, install it from a checkout:

```bash
git clone https://github.com/kith-ai-lab/tret
cd tret/sdk/typescript && npm ci && npm run build
# then, in your app:
npm install /path/to/tret/sdk/typescript
```

Rebuild (`npm run build`) after pulling, since your app links the built
`dist/`.

## Auth

```ts
import { Tret } from '@tret/sdk'

// 1. Bearer: an OIDC access token for the API audience. The server accepts
//    these once TRET_OIDC_API_AUDIENCE is set (see .env.example), and only for
//    a user already linked to that `sub`. A function is called before every
//    request (caching and refresh are yours) and once more after a 401.
const tret = new Tret({
  baseUrl: 'https://tret.example.com',
  auth: { kind: 'bearer', token: () => auth.getAccessToken() },
  workspaceId: '6f1c…', // sent as X-Tret-Workspace
})

// 2. Session cookie: a browser or Electron renderer on tret's origin, or any
//    runtime with a cookie jar. Sends `credentials: 'include'`.
const web = new Tret({ baseUrl: '', auth: { kind: 'session' } })
await web.auth.login({ email, password })

// 3. None: healthz, version and auth.config only.
const anon = new Tret({ baseUrl: 'https://tret.example.com', auth: { kind: 'none' } })
console.log(await anon.version()) // { version: '0.1.0', git_sha: '652dacd…' }
```

**Workspaces.** A session cookie remembers the active workspace. A bearer
caller has no cookie, so pass `workspaceId` (or call
`tret.auth.selectWorkspace(id)`, which also sets it) when the user belongs to
more than one workspace. Otherwise every request answers 409. The server only
honours a workspace the user is a member of, and a cookie session's own
selection always wins over the header.

## The Review stage, end to end

Upload the evidence, run the `climate-risk` pack's `qa_review` task, stream
it, read the finding, record the reviewer's decision and print the receipt.

```ts
import { readFile } from 'node:fs/promises'
import { Tret, TretBudgetRefused, formatReceipt } from '@tret/sdk'

const tret = new Tret({
  baseUrl: process.env.TRET_URL!,
  auth: { kind: 'bearer', token: () => getAccessToken() },
  workspaceId: process.env.TRET_WORKSPACE_ID,
})

// 1. Documents
const files = ['ghg-inventory-2025.pdf', 'tcfd-draft.docx']
const documentIds: string[] = []
for (const name of files) {
  const doc = await tret.documents.upload(await readFile(name), name)
  documentIds.push(doc.id)
}

// 2. The harness that links the climate-risk pack (and so offers qa_review)
const harness = (await tret.harnesses.list()).find((h) => h.pack_slugs.includes('climate-risk'))
if (!harness) throw new Error('no harness links the climate-risk pack')

// 3. Run it, streaming events as they arrive
try {
  const { run, findings, receipt, events } = await tret.runs.complete(
    {
      harnessId: harness.id,
      taskType: 'qa_review',
      documentIds,
      taskInput: { scope: 'FY2025 GHG inventory and TCFD draft' },
    },
    {
      onEvent: (event) => {
        switch (event.type) {
          case 'routing':
            console.log(`→ ${event.data.chosen_model}: ${event.data.reasoning}`)
            break
          case 'tool_call':
            console.log(`→ ${event.data.tool}`)
            break
          case 'finding_recorded':
            console.log(`finding ${event.data.finding_id}`)
            break
        }
      },
    },
  )
  console.log(run.status, `${events.counts.tool_call ?? 0} tool calls`)

  // 4. The finding, and the reviewer's decision on it
  const finding = await tret.findings.get(findings[0]!.id)
  console.log(finding.schema_slug, finding.payload)
  await tret.findings.decide(finding.id, { approved: true, comment: 'Checked against the source PDFs.' })

  // 5. What it cost
  console.log(formatReceipt(receipt))
  // receipt · $0.0412 (+$0.0026 routing) · 1.84 gCO₂e · claude-sonnet-4-5
} catch (error) {
  if (error instanceof TretBudgetRefused) console.error(`refused (${error.reason}): ${error.message}`)
  else throw error
}
```

`complete()` resolves for every run that finishes, `failed` and `cancelled`
included, so check `run.status`. `decide()` needs the `approver` role or
higher in the workspace. The approver recorded is whoever is authenticated;
the API takes no approver field.

## Streaming events yourself

```ts
const { run_id } = await tret.runs.create({ harnessId, taskType: 'qa_review', documentIds })
const controller = new AbortController()
for await (const event of tret.runs.events(run_id, { signal: controller.signal })) {
  if (event.type === 'text_delta') process.stdout.write(event.data.text)
  if (event.type === 'usage') console.log(event.data.cost_usd, event.data.co2e_g)
}
```

`RunEvent` is a discriminated union of every event the backend publishes
(`routing`, `context_composition`, `text_delta`, `tool_call`, `tool_result`,
`finding_recorded`, `usage`, `budget_warning`, `budget_alert`,
`tools_withheld`, `context_pressure`, `compaction`, `model_switch`,
`switch_refused`, `effort_raised`, `provider_ignore_waived`,
`delegation_started`, `delegation_finished`, `delegation_progress`,
`lesson_proposed`, `done`, `error`). Each is `{ type, data, ts }`, where `data`
is the backend payload as sent (snake_case) and `ts` is unix seconds. The
iterator ends after `done` or `error`.

- **Reconnects.** The server replays the whole run from the start on every
  connect and has no `Last-Event-ID`. When a connection drops, the iterator
  reconnects (backoff from `reconnectDelayMs`, up to `maxReconnects` in a row)
  and skips what it already delivered, by position. If the server trimmed the
  backlog in between (runs past 5,000 events), it skips by timestamp instead.
- **Old runs.** The server keeps a finished run's events only while someone
  is reading them and for a short while after. Asking for the events of a run
  it has forgotten gets only keepalives, which arrive every 30 seconds. On the
  first keepalive, the iterator reads the run record and ends with a `done` or
  `error` built from it, marked `synthetic: true`.
- **Stopping.** `break` closes the connection. Aborting `signal` closes it and
  throws an `AbortError`. Neither cancels the run; `tret.runs.cancel(id)` does
  that.
- An event type newer than this SDK is skipped. A test in this package fails
  when the backend adds one, so the union stays complete.

## The Receipt

Mirrors the Python `tret.Receipt` ([docs/embedding.md](../../docs/embedding.md#the-receipt)),
camelCased:

| field | meaning |
|---|---|
| `model` | the tret model id that ran last (after any mid-run switch) |
| `usd` | catalog-priced cost of the run's own tokens |
| `reportedUsd` | the provider-reported cost where one exists, else the catalog price |
| `co2eG`, `co2eGLow`, `co2eGHigh` | estimated grams CO₂e and its judgment band |
| `energyWh` | estimated compute watt-hours (no PUE) |
| `waterMl` | estimated water consumption |
| `baselineModel`, `avoidedUsd`, `avoidedUsdPct`, `avoidedCo2eG`, `avoidedCo2ePct` | the frontier counterfactual, signed |
| `usage` | `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens` |
| `routing` | `chosenModel`, `reasoning`, `candidates`, `fallbackUsed`, `objective` |
| `overhead` | the router call's own spend, kept out of `usd`; `null` when no router model was asked |

**`null` always means "estimate unavailable", never zero.** A run whose
provider reported no usage gets `usd: null`, and `formatReceipt` prints
`receipt · estimate unavailable · <model>` instead of `$0.0000`. The avoided
figures are a model-selection signal, not a booked saving.

## Errors

Every non-2xx response throws a `TretError` with `status`, `detail` (the
response's `detail` field, as sent) and `body`. `message` is the sentence meant
for a person. Subclasses:

| class | when |
|---|---|
| `TretAuthError` | 401, or a 403 that is a role or permission check |
| `TretNotFound` | 404. tret also answers 404 for anything in another workspace. |
| `TretRateLimited` | 429, with `retryAfterSeconds` |
| `TretBudgetRefused` | a gate refused: `{reason, detail}`. Over HTTP, a 402 or 403 with that body. On a run, a pre-run gate (core's spend budget, a hosting extension's credits) does not refuse `POST /api/runs` itself. The run fails before its first model call, and `runs.complete()` throws this with `status: 0` and `runId` set. |
| `TretStreamError` | the event stream could not be re-established |

## API

| | |
|---|---|
| `tret.version()` | `GET /api/version` → `{version, git_sha}`. Falls back to `/api/healthz` (both null) on servers that predate it. |
| `tret.healthz()` | `GET /api/healthz` |
| `tret.auth.config() / me() / login({email, password}) / logout() / selectWorkspace(id)` | `/api/auth/*` |
| `tret.harnesses.list() / get(id)` | `get` adds `assembled_system_prompt` and `task_types` |
| `tret.packs.list() / get(id)` | `get` adds `doctrine_contents` |
| `tret.documents.upload(data, name, mime?) / list() / get(id)` | `data`: `Blob`, `ArrayBuffer` or any typed array (a Node `Buffer` too) |
| `tret.runs.create(opts) / get(id) / list({limit, cursor, topLevelOnly}) / cancel(id)` | `list` always returns `{items, next_cursor}` |
| `tret.runs.events(id, {signal, maxReconnects, reconnectDelayMs})` | `AsyncIterable<RunEvent>` |
| `tret.runs.complete(opts, {onEvent, signal})` | `{run, events, findings, receipt}` |
| `tret.findings.list({runId}) / get(id) / decide(id, {approved, comment})` | |

## Types and the OpenAPI snapshot

Request bodies and the responses the backend declares as pydantic models come
from `src/generated/openapi.ts`, which
[openapi-typescript](https://openapi-ts.dev) generates from the committed
`openapi.json`. Most tret routes return plain dicts, which OpenAPI documents
as an empty schema, so those response types (`RunDetail`, `Finding`,
`HarnessDetail`...) are written by hand in `src/types.ts` against the backend
function each one names. The event union in `src/events.ts` is also written by
hand, because SSE has no OpenAPI schema.

After an API change:

```bash
python backend/scripts/dump_openapi.py sdk/typescript/openapi.json   # from the repo root
cd sdk/typescript && npm run generate
```

CI regenerates both files and fails on any difference.

## Development

```bash
npm ci
npm run typecheck
npm test        # node:test, through tsx
npm run build   # tsup → dist/ (ESM, CJS, .d.ts)
```

Apache-2.0, like the rest of tret.
