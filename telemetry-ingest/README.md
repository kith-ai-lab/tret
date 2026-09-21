# tret-telemetry-ingest

A small Cloudflare Worker that receives opt-in, anonymous telemetry reports
from self-hosted [tret](https://github.com/) instances, stores them in D1,
and serves public aggregates at `telemetry.kithailab.com`.

This source is public on purpose: telemetry is opt-in (off by default), and
anyone should be able to read exactly what the receiving end keeps before
deciding whether to turn it on. The code is plain JavaScript (ES modules, no
TypeScript, no framework) and every privacy-relevant decision has a comment
next to it explaining why.

The payload shape, closed sets, sanity caps and HTTP API this Worker
implements are defined by the shared telemetry contract (payload §3, ingest
API §7, as overridden by the §9 amendments) that also governs the core
reporter. This repo implements the receiving end only.

## What is stored

One row per accepted report, in D1 (`migrations/0001_init.sql`):

- `reports` — the report itself: the UTC *date* it was received
  (`received_day`, never a timestamp), the random `instance_id` it came
  with, the reporting window, `tret_version`/`deploy`/`db`, coarse
  user/workspace/run buckets, rounded token/energy/CO2e figures, and the
  share-map breakdowns (providers, model families, task types, run status,
  factor rungs) and feature flags — all as closed-set values already
  validated by `src/schema.js`.
- `instances` — one row per `instance_id` seen, tracking first/last seen
  day and a pointer to its latest report.
- `totals` / `monthly_totals` — running sums (reports, energy, CO2e,
  tokens), typed per column (`INTEGER` for counts, `REAL` for energy/CO2e),
  bumped on every accepted report, kept independent of `reports` so the
  aggregate history survives the retention purge below.
- `daily_accepts` — one row per UTC day, counting reports accepted that
  day across all instances. Backs the global daily accept cap (see
  Endpoints) and is incremented in the same atomic batch as the report
  insert. Rows older than 40 days are deleted by the retention purge.
- `public_snapshot` — a single stored row holding the exact JSON body
  `GET /v1/aggregates` serves. Rebuilt by the daily cron, never computed
  per request — see Endpoints.
- `purge_state` — a single stored row recording the UTC date the retention
  purge last completed, so a stuck purge is visible on the admin endpoint
  instead of silent.

## What is deliberately not stored

- **No request metadata.** The `POST /v1/report` handler reads only
  `request.method`, the URL path, the `content-type` and `content-length`
  headers, and the body — never `cf`, `CF-Connecting-IP`, `User-Agent`, or
  any other header. The code never reads the requester's IP, country, or
  client string. (`GET /v1/admin/instances` is the one exception: it's
  authenticated, and reads `Authorization`.)
- **No timestamps on reports**, only a UTC calendar date
  (`received_day`), specifically so a report can never be correlated
  against a precise moment in server logs.
- **Cloudflare request logging (`observability`) is off** in
  `wrangler.jsonc`, for the same reason — it would persist per-request
  metadata this Worker otherwise never touches.
- **No raw payload.** `src/schema.js` builds a clean, normalised object
  field by field (allowlist); an unknown top-level field is silently
  dropped, and the raw parsed body is never the thing that gets stored.
  An unknown key *inside* a closed-set map (e.g. an unrecognised provider
  name) is rejected with 400 rather than stored under an unreviewed key —
  and that key is never echoed back in the 400 body, since it's
  attacker-controlled input.
- **No free text, names, hostnames, URLs, API keys, custom model ids,
  grid zone/region, or USD cost** — these were never part of Payload v1 in
  the first place; there's nothing here to strip because the sender never
  sends them.

### What Cloudflare may still see

This Worker's own code never reads or stores IP, country, user agent, or
any other request header on the reporting path, and Workers Logs
(`observability`) is off in `wrangler.jsonc`. That covers everything this
codebase controls. It does **not** cover the Cloudflare zone in front of
it: the zone's own edge analytics, Security Events, and any Logpush job or
WAF/rate-limit rule configured in the dashboard operate independently of
this Worker's code and are outside this repo's scope. In particular, the
edge rate-limit rule recommended below (DEPLOY) is evaluated at the edge,
before a request ever reaches this Worker's `fetch()` handler, and a
request it blocks shows up in Cloudflare Security Events *with an IP* —
that visibility comes from the platform, not from anything this Worker
does.

## Endpoints

- `POST /v1/report` — accepts one Payload v1 report. Requires
  `Content-Type: application/json` (forces a CORS preflight this Worker
  never satisfies, so a browser can't be used as a drive-by reporter).
  `204` stored · `400` schema/sanity-cap violation (the 400 body never
  echoes an attacker-supplied key or value) · `413` body over 8 KB ·
  `415` missing/wrong `Content-Type` · `429` either a report for this
  `instance_id` was already accepted today (UTC), or the global daily
  accept cap (`DAILY_ACCEPT_CAP`, default 2000 reports/day across all
  instances) has been reached · `405` any method but POST. Per-report
  sanity caps on `tokens_in`/`tokens_out`/`energy_wh`/`co2e_g` scale with
  the reporting window (`days = max(1, window length)`): 2e9 tokens/day,
  1e6 Wh-or-g/day. Every non-2xx response carries
  `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.
- `GET /v1/aggregates` — public, CORS `*`, `Cache-Control: public,
  max-age=3600`. Served **verbatim from a stored snapshot** (`body` column
  of `public_snapshot`), never computed against live data for the request
  — an unauthenticated caller polling this endpoint can't difference out
  one instance's individual report between requests. The daily cron
  rebuilds the snapshot only when at least 5 new reports have been
  accepted since the currently published one (or no snapshot exists yet
  and `instances.all_time >= 5`); otherwise yesterday's snapshot stays.
  `totals` and `monthly` are `null` until `instances.all_time >= 5`.
  `breakdowns` (`versions`, `deploy`, `providers`, `model_families`,
  `factor_rungs`, `features`) stay `null` until at least 10 *seasoned,
  active* instances exist — active = last report within 35 days, seasoned
  = first seen at least 7 days ago — then each is a map of key →
  seasoned-active-instance count (share-map keys only counted when that
  instance's share for the key is > 0), with any key backed by fewer than
  5 instances removed and counted in `_suppressed`. Before any snapshot
  has ever been built, the endpoint serves an all-zero/empty shape with
  `totals`, `monthly`, `breakdowns` and `snapshot_day` all `null`.
  `generated_at` is the snapshot's build time, and the response adds
  `"snapshot_day": "YYYY-MM-DD"`.
- `OPTIONS /v1/aggregates` — CORS preflight for the above.
- `GET /v1/admin/instances` — private. `Authorization: Bearer <ADMIN_TOKEN>`
  (scheme matched case-insensitively, token trimmed), constant-time
  compared. `503` if the secret isn't configured, `401` if it's wrong or
  missing. Every response (200, 401 or 503) carries `Cache-Control:
  no-store` and `Vary: Authorization`. Unfloored fleet data for the
  internal admin console, computed live (this endpoint is authenticated,
  not the one an anonymous observer could poll) — never expose this
  response publicly. Counts *every* instance, with no activity or
  seasoning filter (unlike the public breakdowns). Adds `last_purge_day`
  and `snapshot_day` so a stuck purge or a snapshot that's stopped
  rebuilding is visible rather than silent.
- A daily cron (`scheduled()`, see `src/retention.js` and
  `src/aggregates.js`) runs the retention purge — deletes `reports` older
  than 13 months, `instances` not seen for 13 months, and `daily_accepts`
  rows older than 40 days, writing `last_purge_day` in the same atomic
  batch as those deletes — and then, only if the rebuild rule above is
  met, rebuilds the public snapshot. The two steps each run in their own
  try/catch, so one failing (e.g. the purge) never blocks the other.
  `totals`/`monthly_totals` are never touched by the purge.

### Known limitation: reports are unauthenticated

`POST /v1/report` has no auth — by design, since a self-hosted instance
has no credential to present. That means someone willing to submit fake
reports over time can still skew totals or probe the 5/10-instance floors
(e.g. submit 4 fake reports with a distinctive `deploy` value, and if a
real report makes it 5 and the key becomes visible, you've confirmed one
real instance uses it). The mitigations are the caps above (the
`Content-Type` requirement, the global daily accept cap, the window-scaled
per-report caps), the 7-day seasoning rule, the daily blended snapshot
(every published change blends at least 5 reports), and a Cloudflare edge
rate-limit rule on `/v1/report` (see DEPLOY) — evaluated at the edge,
before this Worker's code ever runs, and independent of anything in this
repo. None of this makes the figures authenticated: they remain
self-reported.

## Recomputing totals

`totals`/`monthly_totals` are running sums, not derived from `reports` at
read time (that's what lets them survive the retention purge). Within the
retained window (last 13 months of `reports`), they can be independently
recomputed and cross-checked with:

```sql
SELECT COUNT(*) AS reports,
       SUM(energy_wh) AS energy_wh,
       SUM(co2e_g)    AS co2e_g,
       SUM(tokens_in) AS tokens_in,
       SUM(tokens_out) AS tokens_out
FROM reports;
```

This will only match `totals` exactly if no report older than 13 months
has been purged since the Worker was deployed; once retention has purged
anything, `totals` (the true all-time running sum) will be higher than
this query's retained-window recomputation, by construction.

## Project layout

```
wrangler.jsonc          Worker config: D1 binding, custom domain, cron, DAILY_ACCEPT_CAP var, observability off
migrations/0001_init.sql D1 schema
src/index.js             Router + report insert (single atomic env.DB.batch) + scheduled() orchestration
src/schema.js             Payload v1 validator (closed sets, window-scaled caps, allowlist build)
src/aggregates.js         Public snapshot rebuild/read + admin aggregate query builder
src/admin.js               Constant-time bearer auth for the admin endpoint
src/retention.js           Daily retention purge
src/dates.js                UTC date helpers shared by the above
test/                        vitest + @cloudflare/vitest-pool-workers (real local D1)
```

## Running the tests

```
npm install
npm test
```

Tests run against a real local D1 instance via
`@cloudflare/vitest-pool-workers` (migrations applied automatically before
each test file — see `vitest.config.js` / `test/apply-migrations.js`), not
a mock, so D1-specific behavior (the unique-constraint 429, the upsert,
batch-as-transaction rollback) is exercised for real.

## DEPLOY (maintainer)

This Worker must be deployed from the **Kith/Voiz Cloudflare account that
owns the `kithailab.com` zone** — the custom-domain route in
`wrangler.jsonc` will fail to attach otherwise. Check first:

```
npx wrangler whoami
```

Requires Node 22+. Then, one time:

```
npx wrangler d1 create tret-telemetry
# paste the returned database_id into wrangler.jsonc's "database_id" field
npx wrangler d1 migrations apply tret-telemetry --remote
npx wrangler secret put ADMIN_TOKEN
npx wrangler deploy
```

`ADMIN_TOKEN` is a secret (not a `vars` entry) precisely so it never lands
in `wrangler.jsonc` or source control — see the comment next to it in that
file. `DAILY_ACCEPT_CAP` is a plain `vars` entry in `wrangler.jsonc`
(default `"2000"`) — an operational knob, not sensitive, so it's fine
committed; adjust it there and redeploy if the default cap is wrong for
your fleet size.

Then, in the Cloudflare dashboard (not in this repo): create a rate
limiting rule for `POST telemetry.kithailab.com/v1/report` — e.g. 5
requests per hour per IP, action block. This is the edge mitigation
referenced in "Known limitation: reports are unauthenticated" above: it's
evaluated at the edge, before a request reaches this Worker's code, and a
blocked request appears in Cloudflare Security Events (with the requesting
IP) purely as a platform feature — this Worker's own code still never
reads or stores that IP.

### Forks

If you're running your own instance of this ingest Worker (e.g. because
you've repointed `TRET_TELEMETRY_URL` at your own host), the steps are the
same, minus the "must be the Kith/Voiz account" constraint: create your own
D1 database, apply the migration, set your own `ADMIN_TOKEN`, and either
point the `routes` entry at a domain you control or remove it and deploy to
a `*.workers.dev` subdomain instead.
