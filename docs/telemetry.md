# Telemetry

tret can send small, anonymous, aggregate usage reports to Kith. It is off
until an admin turns it on, and this page is what a self-hoster should read
before deciding whether to.

## What it is, and why Kith asks

tret is free, self-hosted software, so Kith otherwise has no way to know how
many deployments exist, what they run, or how much energy and CO2e the whole
open-source fleet represents. Telemetry closes that gap without opening a
channel into any deployment's actual data: an instance that opts in reports a
handful of bucketed counts and proportions on a schedule — never prompts,
outputs, transcripts, or anything that identifies a person, a workspace, or
an organization. Kith's stated use for it is to publish the fleet's combined
energy and CO2e totals as open, citable data (`GET /v1/aggregates`, see
[What Kith does with a report](#what-kith-does-with-a-report)) and to see
which providers, deploy shapes, and features are actually in use, so
engineering effort follows real usage instead of guesses. Nothing here is
billed, gated behind, or gives Kith a lever over your deployment.

## Off by default

`TRET_TELEMETRY` (Settings field `telemetry`) has three values:

| value | meaning |
| --- | --- |
| `admin` (default) | The instance admin decides, in the UI or CLI. Off until they turn it on — a DB toggle, `false` by default. |
| `off` | Locked off by the operator. The UI toggle is disabled and the DB toggle is ignored. |
| `on` | Locked on by the operator, for headless installs. The UI shows it as on and locked; the DB toggle is ignored. |

Two more inputs narrow it further:

- `TRET_TELEMETRY_URL` — where a report is POSTed. Defaults to
  `https://telemetry.kithailab.com/v1/report`. An empty string locks
  telemetry off, full stop — see [Forks](#forks).
- `DO_NOT_TRACK` — a plain (no `TRET_` prefix) environment variable read from
  the process environment, honored the way it is elsewhere in the ecosystem.
  Any value other than empty, `0`, or `false` locks telemetry off, and this
  beats every other setting, including `TRET_TELEMETRY=on`. Under Docker,
  `docker-compose.yml` passes it through to the container
  (`DO_NOT_TRACK: ${DO_NOT_TRACK:-}`), so set it in your `.env` file rather
  than the shell you happen to run `docker compose` from.

The egress class `telemetry` (see [hardening.md](hardening.md#9-outbound-network-what-tret-may-talk-to))
has to be in force too: if the master egress switch or that class is off,
telemetry is off regardless of `TRET_TELEMETRY`.

Put together, the effective state is resolved in this order — first match
wins, and each one is recorded as `locked_reason` on `GET
/api/admin/telemetry`:

1. `DO_NOT_TRACK` is set → off, `do_not_track`
2. an extension override says off → off, `extension` (this is how Tret Cloud
   forces itself off — see [Tret Cloud](#tret-cloud))
3. `TRET_TELEMETRY=off` → off, `env_off`
4. `TRET_TELEMETRY_URL` is blank → off, `no_url`
5. the `telemetry` egress class is not `on` → off, `egress_off`
6. `TRET_TELEMETRY=on` → **on**, locked, `env_on`
7. `TRET_TELEMETRY=admin` → the DB toggle decides; `locked_reason` is null

## Turning it on or off

**Settings UI** — the "Anonymous usage statistics" card (admin only) shows
the current effective state, lets you flip the DB toggle when nothing else
has it locked, and shows exactly why it's locked when something does.

**CLI** — `tret telemetry status`, `tret telemetry preview`, `tret telemetry
enable`, `tret telemetry disable`. `status` and `preview` print JSON to
stdout, so they're scriptable; `preview` never mints an instance id (see
below) just for showing you a payload.

**Makefile** — `make telemetry-preview` wraps the CLI's preview for anyone
who reaches for `make` first.

## Payload v1

This is the entire body tret ever POSTs to `telemetry_url` — one JSON object,
`Content-Type: application/json`, no auth header, no cookies, `User-Agent:
tret-telemetry/1`. The payload is built with a schema that rejects unknown
fields (Pydantic, `extra="forbid"`), so nothing can be added to it without a
code change to this document's field list:

```json
{
  "schema_version": 1,
  "instance_id": "uuid4 string",
  "window_start": "2026-09-14",
  "window_end": "2026-09-21",
  "tret_version": "0.1.0",
  "deploy": "docker",
  "db": "postgres-16",
  "users_bucket": "2-5",
  "workspaces_bucket": "1",
  "runs_bucket": "101-1000",
  "tokens_in": 1200000,
  "tokens_out": 340000,
  "providers": {"anthropic": 0.6, "local": 0.4},
  "model_families": {"claude": 0.6, "llama": 0.4},
  "task_types": {"chat": 0.7, "pack": 0.3},
  "run_status": {"completed": 0.94, "failed": 0.06},
  "energy_wh": 5300.0,
  "co2e_g": 2100.0,
  "factor_rungs": {"provider": 0.7, "global_default": 0.3},
  "features": {"packs": true, "connections": false, "delegation": true, "local_models": true}
}
```

| Field | Where it comes from | Privacy treatment |
| --- | --- | --- |
| `schema_version` | fixed constant for this payload shape | lets an older ingest Worker and a newer core stay compatible; not itself sensitive |
| `instance_id` | a random UUIDv4, minted when telemetry becomes effectively on | never derived from a name, hostname, MAC address, or any other identifier — see [Instance id lifecycle](#instance-id-lifecycle) |
| `window_start` / `window_end` | the report's coverage window: from the last successful send (or seven days back, on the first report), capped at 35 days | UTC dates only, no timestamps |
| `tret_version` | the `tret.__version__` constant | — |
| `deploy` | detected from the environment (`FLY_APP_NAME` → `fly`, `RENDER` → `render`, `/.dockerenv` → `docker`, else `bare`) | one of a closed set of four values, never a hostname or platform account |
| `db` | the database dialect and major version tret is connected to | `postgres-<major>` or `sqlite` only — never a connection string or hostname |
| `users_bucket`, `workspaces_bucket` | counted, then bucketed | one of `0 \| 1 \| 2-5 \| 6-20 \| 21-100 \| 100+` — never an exact count |
| `runs_bucket` | runs in the window, bucketed the same way | `0 \| 1-10 \| 11-100 \| 101-1000 \| 1001-10000 \| 10000+` |
| `tokens_in`, `tokens_out` | summed over runs in the window | rounded to 2 significant figures |
| `providers`, `model_families`, `task_types`, `run_status` | share of runs in the window, per key | closed key sets (below); a value outside the set folds into a fallback key; keys at 0 are omitted, `{}` when there are no runs in the window |
| `energy_wh`, `co2e_g` | summed from the same per-run reading the emissions analytics uses | rounded to 2 significant figures, or `null` when nothing in the window carries a figure. `co2e_g` is *also* `null` whenever the window mixes location-based and market-based grid factors — the same rule the in-app emissions analytics uses to refuse summing carbon across differing GHG Protocol bases; `energy_wh` has no such basis and is still reported even when `co2e_g` is null. |
| `factor_rungs` | which *kind* of grid factor a run's carbon number used, from `energy_accounting["grid_co2e_source"]` | only the text before the first `:` — the layer's name (`run_override`, `harness`, `workspace`, `managed`, `env`, `dataset`, `provider`, `local_setting` or `global_default`), never the provider, managed-source, zone or region text after it |
| `features` | four booleans: `packs` (at least one operator-installed pack exists, outside the built-in pack directories — a fact about the deployment, not the report window), `connections` (a connection row exists — also deployment-wide), `delegation` (a run in the window has `parent_run_id`), `local_models` (a run in the window used `provider_used == "local"`) | presence only — never which pack, which connection, or which model |

Closed key sets for the share maps:

- `providers`: `anthropic \| kimi \| openrouter \| local \| other`
- `model_families`: matched by substring on the lowercased model name, first
  match wins — `claude, gpt, gemini, gemma, llama, mistral, mixtral→mistral,
  qwen, deepseek, kimi, moonshot→kimi, grok, phi, command`; no match folds to
  `local/other` when the provider is `local`, otherwise `other`. A custom or
  private model name is never emitted, matched or not.
- `task_types`: `chat \| freeform \| pack` (anything that is not chat or
  freeform counts as `pack`)
- `run_status`: `queued \| running \| completed \| completed_without_output \|
  failed \| cancelled \| other`
- `factor_rungs`: `run_override \| harness \| workspace \| managed \| env \|
  dataset \| provider \| local_setting \| global_default`; missing or null folds
  to `legacy`, anything else to `other`

## What is never sent

- prompts, outputs, transcripts, or any other conversation content
- emails, display names, workspace, project, harness, or pack names
- hostnames, URLs, API keys, or custom/private model ids
- grid zone or region
- cost in USD
- any free-text column
- exact counts of users, workspaces, or runs — only the bucketed ranges above
  ever leave the instance

Two of those are worth explaining, since a narrower report than "everything
except these" was clearly possible and deliberately not built:

- **Grid zone or region.** For a self-hosted deployment, a grid zone or
  region is close to a physical location. Telemetry is not going to be the
  thing that narrows down where an instance — and the data behind it — is
  actually running, even in aggregate. `factor_rungs` reports *which kind* of
  carbon factor a run used, never the zone code, region, or provider text
  that names it.
- **Cost in USD.** What a deployment spends is that operator's business, not
  Kith's, and it isn't needed for what these reports are for. Energy and
  CO2e already answer "how much compute did the fleet use"; what that cost a
  given operator has no bearing on the adoption question or on the published
  totals, and including it would be collecting a sensitive number for no
  purpose it serves.

## Cadence and failure behavior

Sending happens on one background task, started in the app lifespan next to
tret's other startup work and cancelled the same way on shutdown. It waits a
random 5–60 minutes after boot before its first check — so a fleet that
booted at the same moment doesn't all hit the ingest endpoint at once — then
checks every 6 hours: it builds a payload and POSTs it when telemetry is
effectively on **and** both of these are true — the last successful send is
missing or more than 7 days old, *and* the last attempt of any kind (sent or
failed) is missing or more than 24 hours old. That second condition is what
stops a permanently-failing collector from being retried every 6-hour tick
forever: after any attempt, successful or not, the instance backs off for at
least 24 hours before trying again; only a genuine success resets the 7-day
send clock. If an instance runs several app worker processes, each one starts
its own sender task and resolves state independently — the ingest Worker's
one-report-per-instance-per-day rule (see
[What Kith does with a report](#what-kith-does-with-a-report)) is what
absorbs the resulting duplicates, not anything on the sending side.

The request itself is deliberately unforgiving: a 5-second timeout, no
retries, and redirects are not followed. Any failure — timeout, a non-2xx
response, a network error — is swallowed and logged at `DEBUG`; nothing about
a failed send is allowed to raise into the lifespan or delay shutdown. A
failed attempt is still recorded in the last-10 log described below, so a
string of failures is visible to the admin even though the process itself
never complains about them.

Preview and send share one code path — the same `build_payload()` call
produces the JSON the admin previews and the bytes actually POSTed — so
"what would be sent" is never a separate, potentially stale, description of
the real thing.

## Instance id lifecycle

The instance id is a random UUIDv4, and nothing else. It is minted when an
admin turns telemetry on in the UI or CLI, or — when the operator has set
`TRET_TELEMETRY=on` — on the first send. Turning telemetry off through the
UI or CLI deletes the id immediately. When telemetry is instead locked off
by the operator (`TRET_TELEMETRY=off`, `DO_NOT_TRACK`, a blank
`TRET_TELEMETRY_URL`, an extension override, or the `telemetry` egress class
off), the id is deleted at the next startup and at every 6-hourly sender
check; since those lock reasons come from an env change that needs a
restart to take effect, in practice that means at that restart. The local
last-10 log is not deleted alongside it — it is local-only history, not
something Kith holds.

Re-enabling after a disable mints a **new** id; the old and new ids share no
common identifier, so nothing in the payload itself lets Kith connect a
re-enabled instance's reports to its earlier ones.

## Verifying it yourself

Nothing above should have to be taken on faith:

- `GET /api/admin/telemetry/preview` shows the exact payload that would be
  sent right now — identical to what a real send POSTs except for
  `instance_id`, which reads `(minted when enabled)` until telemetry is
  actually on — without sending it or minting an id.
- `GET /api/admin/telemetry` carries `recent`, the last 10 send attempts
  (sent or failed, with the payload each one carried), and the Settings card
  surfaces the same log.
- The `telemetry` egress class shows up in the egress report
  (`GET /api/analytics/guardrails`), in `tret egress status`, and in the boot
  log, with `telemetry.kithailab.com` as its one allowed host — see
  [hardening.md](hardening.md#9-outbound-network-what-tret-may-talk-to). On a
  default install that class shows as *open*, because `TRET_TELEMETRY=admin`
  leaves the door available for an admin to walk through — but an open
  network path is not the same thing as telemetry being on. Whether a report
  has ever actually been sent is `tret telemetry status` (or the Settings
  card), not the egress report.
- To watch the actual traffic: route the instance through a proxy
  (`TRET_EGRESS_PROXY`) or run `tcpdump` for `telemetry.kithailab.com` — with
  telemetry off, or between its once-per-6-hours checks, you should see
  nothing to that host at all.

## Ingest rules a self-hoster might hit

`POST /v1/report` is unforgiving about the shape of a request, on purpose —
these are the ones you could actually run into (from your own client, or a
fork's):

- `Content-Type: application/json` is required, or the request is rejected
  with `415` before the body is even read. This also forces a CORS
  preflight the Worker never satisfies, so a browser page can't be turned
  into a drive-by reporter.
- The body is capped at 8 KB; over that is `413`.
- At most one report per `instance_id` per UTC day is accepted; a second one
  the same day is `429`.
- A global daily accept cap (`DAILY_ACCEPT_CAP`, default 2000 reports/day
  across every instance) also returns `429` once reached, independent of
  which instance is asking.
- Per-report sanity caps scale with the reporting window
  (`days = max(1, window length)`): `tokens_in`/`tokens_out` up to `2e9 ×
  days`, `energy_wh`/`co2e_g` up to `1e6 × days`. A window-length caveat
  worth knowing if you ever hand-build a report against a repointed
  `telemetry_url`.
- A value outside a closed set (an unrecognised `provider`, `deploy`, etc.)
  is `400`, not silently accepted under an unreviewed key — except an
  *unknown top-level field*, which is dropped rather than rejected, so an
  older Worker keeps working with a newer core. A `400` body never echoes
  back the value that failed.

## What Kith does with a report

The ingest Worker (`POST /v1/report`) never stores the request's IP address,
user agent, country, or any other header — those are simply never written
down, not merely scrubbed after the fact. Raw reports are kept for 13 months
and then purged by a daily cron, along with any instance not seen in that
window; running totals and monthly rollups live in their own table so they
survive that purge.

`GET /v1/aggregates` is the public result, served with
`Access-Control-Allow-Origin: *` and an hour of caching — but never computed
against live data for the request. It's served verbatim from a stored
snapshot, rebuilt by a daily job, and that job only rebuilds once at least 5
new reports have been accepted since the snapshot currently published (or,
before any snapshot exists, once 5 instances have ever reported at all);
otherwise the previous day's snapshot keeps serving. That means every
published change to the public numbers blends at least 5 reports together,
and an unauthenticated caller polling the endpoint can never difference out
one instance's individual report between two requests. The response carries
a `snapshot_day` field naming the day that snapshot was built (`null` before
the first one exists).

`totals` and `monthly` stay `null` until at least 5 instances have ever
opted in, full stop. `breakdowns` (`versions`, `deploy`, `providers`,
`model_families`, `factor_rungs`, `features`) stay `null` until at least 10
instances are both *active* (reported within the last 35 days) and
*seasoned* (first seen at least 7 days ago) — a brand-new instance's first
few reports don't move a public breakdown. Once shown, any individual key
still backed by fewer than 5 such instances is dropped from the map and
rolled into a `_suppressed` key instead of being shown on its own. All of
this is a property of the public endpoint, not of the stored data: Kith's
own admin console reads a separate, private `GET /v1/admin/instances`
endpoint (bearer-protected, source in
[`telemetry-ingest/src/`](../telemetry-ingest/src/)) that returns the same
fleet breakdowns unfloored and live, for Kith's internal use only — it is
never exposed publicly.

The Worker's full source ships in this repository, at
[`telemetry-ingest/`](../telemetry-ingest/) — reading it is the actual
answer to "what does Kith do with this," more authoritative than this page.

## Limits of anonymous reporting

Worth being honest about: `POST /v1/report` has no auth, by design — a
self-hosted instance has no credential to present. That means someone
willing to submit fake reports over time could still skew the published
totals, or probe where the 5/10-instance floors sit. The mitigations are the
caps above, the 7-day seasoning rule, the fact that every published snapshot
blends at least 5 real reports, and a Cloudflare rate-limit rule on
`/v1/report` configured at the edge — evaluated before a request ever
reaches the Worker's own code, and independent of anything in this
repository (see
[`telemetry-ingest/README.md`'s "What Cloudflare may still see"](../telemetry-ingest/README.md#what-cloudflare-may-still-see):
the Worker itself never reads or stores an IP, but Cloudflare's own edge, like
any CDN in front of it, still sees the connecting IP for that edge rule to
act on). None of this makes the figures authenticated — they remain
self-reported.

## Forks

`TRET_TELEMETRY_URL` is exactly how a fork of tret repoints reporting at its
own endpoint instead of Kith's — the `telemetry` egress class allows both the
default `telemetry.kithailab.com` and whatever host `telemetry_url` names, so
a repointed URL is not blocked by the egress layer. Set it to an empty string
instead, and telemetry is locked off for that fork (`no_url`, from the
resolution order above), independent of anything else in this document.

## Tret Cloud

Tret Cloud never reports, and cannot be made to: it registers an extension
override that always returns `"off"`
(`ext.add_telemetry_override(lambda: "off")`), which is the `extension`
reason in the resolution order above and beats every other setting except
`DO_NOT_TRACK`. Usage on Tret Cloud is covered by that hosted service's own
terms, not by this document.

## Schema changes

A change to the payload shape bumps `schema_version`, and a snapshot test in
the core test suite catches any such change: it fails whenever the payload's
fields change shape, and its failure message tells the developer to update
this document's field table and the ingest Worker's schema to match. The
test doesn't read or parse this document, so it can't verify the table above
is actually accurate — it only guarantees that a shape change can't land
silently. If you're reading this after such a bump and the table above looks
wrong, that's a gap the test's message was meant to prevent, not one it
directly caught.
