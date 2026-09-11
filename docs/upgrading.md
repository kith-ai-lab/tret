# Upgrading tret

Short version: **pull the new version and start it.** tret migrates its own
database on boot, including adopting a database created by a release that did not
yet use migrations. Nothing to run by hand, and nothing is dropped.

The rest of this page is what happens under that sentence, and what to do in the
one case tret refuses to act on its own.

## What happens automatically

On every startup, before serving a single request, tret brings the schema to the
current Alembic head (`backend/tret/db/migrate.py`). It recognises three states:

| State | What tret sees | What it does |
| --- | --- | --- |
| **Empty** | no tret tables | `alembic upgrade head` — builds the whole schema from the first revision |
| **Stamped** | `alembic_version` holds a revision | `alembic upgrade head` — applies whatever is outstanding, a no-op when current |
| **Legacy** | tret tables present, `alembic_version` absent | infers the baseline revision from the schema, `alembic stamp`s it, then upgrades |

The step runs under a Postgres advisory lock, so two instances started together
serialise: one migrates, the others wait and then find the work done. Migrations
apply in a single transaction — Postgres DDL is transactional, so a failure part
way leaves the database exactly as it was.

Startup logs say what was decided:

```
INFO  [tret.schema] schema state: stamped (alembic_version = f4c1d8ab26e7)
INFO  [tret.schema] schema is at revision f4c1d8ab26e7
```

## 2026-09-11 · chat and runs fixes

Three fixes, no migration:

- **A failed chat turn no longer shows the previous turn's reply.**
  `_assistant_message` (`backend/tret/api/chat.py`) scanned all of
  `run.messages` for "the last assistant text", but `run.messages` begins
  with the conversation history the engine seeds onto the front of every
  chat run (`task_input["_history"]`) — so a run that failed before writing
  anything of its own surfaced the *prior* turn's reply as if it were this
  one's (seen on cloud: runs `3ccb2fd2`, `8474f908`). The scan now skips
  exactly that history prefix (accounting for however much of it the
  engine's own context-budget trim dropped, via `run.compactions`); a failed
  run with no text of its own again shows "(run failed: …)".
- **An empty lookup or search is now visible in the chat activity pills**,
  instead of rendering identically to one that found something. A tool
  call's activity entry now carries a `summary` of `"no rows matched"`
  (`lookup_dataset`), `"no matches"` (`search_documents` /
  `search_connected_files`), or `"error"` (the tool result's
  `meta.error`) — delegation's existing `"delegated <task_type>"` summary is
  unaffected. The chat pill (`frontend/src/views/Chat.tsx`) now shows the
  tool name and, when present, the summary after it.
- **`GET /api/runs` now pages.** It returned at most 50/200 rows with no way
  to see the rest. It now accepts `limit` (1–200, default 50) and `cursor`
  and, when either is passed, returns `{"items": [...], "next_cursor": ...}`
  — keyset pagination on `(created_at desc, id desc)`, so a run created
  mid-page never shifts or duplicates a row across pages. Calling it with
  neither parameter still returns the original bare list, unchanged, for any
  caller that never updates. The Runs page (`frontend/src/views/Runs.tsx`)
  now fetches 50 at a time behind a "Load more" button instead of 200 in one
  shot.

## Chat replies get a grounding check (2026-09-11)

Chat and freeform prose had no equivalent of the `cited_values` cross-check a
verdict task's structured output gets — a model could state a number in plain
text that no `lookup_dataset` call this run returned, and nothing noticed. The
engine now checks every number in a chat/freeform reply against what the run
actually retrieved and what the conversation actually said
(`tret/engine/grounding.py`); an unsupported figure gets the reply sent back
for a rewrite, up to two rewrites, and a third bad reply ships as the model
wrote it, flagged rather than blanked. See the new paragraph in
`docs/trust-doctrine.md` after the cited-values one.

This adds a nullable `runs.grounding` column, applied by the automatic
migration step above like any other — nothing to run by hand. It is null for
every run that predates this (nothing to backfill: the check never ran) and
for any run that still isn't chat/freeform. `GET /api/runs/{id}` and a chat
message's assistant entry both now carry a `grounding` field
(`{checked, status, attempts, unsupported, first_unsupported}`) whose `status`
is one of `clean`, `repaired`, `unresolved`, or `skipped` (`checked: False`) —
a run that never called a tool and never retrieved anything has nothing to
check a reply's numbers against, so it is recorded `skipped` rather than
either `clean` (which would claim a check that never ran) or silently null
(indistinguishable from a run that predates this feature). The chat UI shows
a line when a reply shipped unresolved or had to be rewritten.

## OpenRouter `require_parameters` is now opt-in (2026-09-11)

Until this change every OpenRouter request that carried tools also sent
`provider.require_parameters: true`. OpenRouter then dropped every endpoint
that does not advertise the full parameter set, and for the OpenAI models that
was all of them: tool-calling runs on `gpt-5.6-luna`, `gpt-5.6-terra` and
`gpt-6-astra` failed at iteration 0 with "No endpoints found that can handle
the requested parameters" while the router's own tool-less call succeeded.

Nothing is sent by default now. The engine already validates tool calls and
JSON output in-loop, which catches a provider that silently drops tools. If
you still want OpenRouter to pre-filter endpoints, opt in:

```
TRET_OPENROUTER_PROVIDER_PREFS={"require_parameters":true}
```

No migration, no restart beyond the deploy.

## 2026-09-11 · providers and routing

Four fixes, no migration:

- **A transient upstream failure is now retried once**, in the OpenAI-compatible
  provider (`backend/tret/providers/openai_compat.py`, used by OpenRouter and
  Kimi) and in `AnthropicProvider`. Over roughly 330 cloud runs, three Google AI
  Studio 503s relayed through OpenRouter and one OpenRouter HTML error page (a
  Cloudflare template, stored verbatim as `run.error`) each failed a run outright
  when a single retry would very likely have recovered them. On an HTTP
  502/503/504/529, or any 5xx whose body is HTML, the provider now waits 2s and
  retries the request once before raising; a partial stream (any token already
  yielded to the caller) is never retried, since the caller may already have
  acted on it. An HTML error body is now summarized to `[<provider>] upstream
  returned HTML (HTTP <status>[, <title>])` instead of being stored as the raw
  page source.
- **A model whose endpoint has just started rejecting every request is now
  excluded from routing across every task shape and objective for a while** —
  a circuit breaker, not the existing per-key priors (which are scoped to one
  `(task_shape, objective, size_band)` and so never generalized: `gpt-5.6-luna`
  was demoted for chat shapes but kept getting chosen, and failing, for
  extraction and verdict runs). When a model's last two runs within the last
  `TRET_ROUTER_COOLDOWN_MINUTES` (default 30, `0` disables) both failed at
  iteration 0 with a provider error, it sits out routing for that long,
  recorded on the run's `routing.evidence.cooldown`. Never excludes every
  candidate — if doing so would leave nothing to route to, the cooldown is
  ignored and the decision's `reasoning` says so.
- **Verdict-shape reasoning effort now follows the chosen model's own cost
  tier**, not a flat per-objective value. `gemini-3.8-flash` (a `standard`-tier
  model) spent 31k–74k output tokens and $0.14–0.34 per verdict case at the
  previous flat `high` default without landing more designed cases than a
  cheaper effort would have, while `low` under the `eco` objective landed the
  hardest case in the same batch. `premium` still gets `high`, `standard` now
  gets `medium`, and `economy`/`local` get `low` — for every objective except
  `quality`, which keeps `high` on `standard` too. See
  [eco-accounting.md](eco-accounting.md#reasoning-effort-and-the-verdict-shapes-own-tier-table).
- **`token_conservation` runs now tell the model itself to conserve tokens.**
  The objective already picked a cheap model and capped its reasoning effort,
  but neither touched what the model was told about the turn it was running —
  and on two drafting-shape runs the cheap model wrote *more* than a `balanced`
  run on the same prompt. The context assembler now appends an
  `objective_guidance` block (accounted in `context_composition` like every
  other block) asking for the fewest words that fully answer the task, no
  preamble, a short list over prose, and exact quoted values. See
  [eco-accounting.md](eco-accounting.md#telling-the-model-itself-to-conserve-tokens).

No action needed on upgrade; `TRET_ROUTER_COOLDOWN_MINUTES` is optional (see
`.env.example`).

## 2026-09-11 · cloud, deploy and pack

No migration. Three independent fixes:

- **Shutdown now drains in-flight runs before exiting.** Two live deploys
  each failed a run that was still genuinely in progress with
  `process_restart: the server restarted while this run was in progress` —
  the run hadn't crashed, it just hadn't finished by the moment the old
  process exited, so the new process's startup sweep found its row still
  `running` and closed it out as orphaned. `tret/main.py`'s lifespan
  shutdown now waits up to `TRET_SHUTDOWN_DRAIN_SECONDS` (default 45s) for
  runs still executing to reach their own terminal state before this process
  exits, polling once a second; only a run still going once that deadline
  passes is left non-terminal for the sweep to catch. If you run tret behind
  an orchestrator that sends a hard kill after some timeout (Fly's
  `kill_timeout`, Kubernetes' `terminationGracePeriodSeconds`, ...), make
  sure that timeout is at least `TRET_SHUTDOWN_DRAIN_SECONDS` or the process
  is killed out from under the wait before it can do any good — tret-cloud's
  own `fly.toml` now sets `kill_timeout = "60s"` to cover the 45s default
  with margin. See `.env.example` for the new setting.
- **`GET /api/billing/status` (tret-cloud) now reports `held_usd` and
  `available_usd`** alongside the existing `balance_usd`, so a tester (or
  the billing UI) can see an in-flight run's credit reservation instead of
  it being invisible until the run finishes. Additive fields only — nothing
  existing changes shape. See tret-cloud's own README for details.
- **The climate-risk pack's reason-code doctrine now distinguishes
  `methodology_choice` from `site_specific_factor`** when a vendor's method
  note names a technique (terrain amplification, resolution, a modelling
  choice) that happens to reference a site property — see
  `packs/climate-risk/doctrine/03-reason-codes.md`. Pack version bumped
  0.1.0 → 0.1.1. **A workspace that already has the pack installed keeps
  0.1.0 — and the old doctrine — until it reinstalls**; the pack version
  bump alone does not retroactively change what an installed workspace's
  runs see. Also in this pack: `methods/ghg_inventory.py`'s scope1_mobile
  factor is now diesel- or gasoline-specific (matched from each record's
  `activity`/`notes` text) instead of a single diesel-only number, which
  used to over-count a gasoline record by about 16%; every existing
  installed workspace's own workbook data is unaffected until it reinstalls,
  same as the reason-code change above.

## 2026-09-11 · documents, exports and the grounding nudge

No migration. Five independent fixes:

- **Chat can now reach a project's documents even when none are explicitly
  attached.** `search_documents`/`read_document` (`tret/engine/tools.py`)
  used to see only `run.document_ids`, and a chat run always has none — so a
  chat user asking about a document already sitting in their project always
  got "No documents are attached to this run". A chat/freeform run with
  nothing attached now searches (and may read) every document of its own
  project instead, scoped by `project_id` so a document belonging to another
  project is never reachable this way. A specialist task (a divergence
  assessment, an evidence extraction with nothing attached, ...) keeps the
  old, narrower behaviour unchanged.
- **The grounding nudge no longer reads as "throw away your whole reply and
  re-look-up everything."** Two problems in the wording, seen on cloud: a
  repaired reply sometimes dropped its actual substance along with the
  unsupported numbers (down to "I am ready to assist…"), and a nudge could
  trigger six to nine fresh tool calls even though the data it needed was
  already in the conversation. `grounding_nudge_message`
  (`tret/engine/grounding.py`) now says plainly that the retrieved data for
  this turn is already there and the model must not query again for it,
  that a value it cannot support should be stated as unavailable (filing a
  data request if the task needs it), and that the rewrite must still
  answer the user's question in full.
- **A `.pptx` extraction failure now says more than "text extraction
  failed: error".** `extract_bounded` (`tret/services/documents.py`) always
  reported the exception's class name for every format; for `.pptx`
  specifically it now also appends the first line of the exception's own
  message (`python-pptx`'s own exceptions — a missing package member, a
  malformed part — usually have one worth showing), so a reviewer sees
  *what* about the file broke, not just that something did.
- **An approved deliverable section no longer ships its own "this is a
  draft" banner.** A `draft_section` finding's body sometimes opens with a
  model-written `**Draft status:** This section is a draft awaiting review
  by a named human reviewer…` paragraph — true while the finding actually
  is a draft, false and confusing the moment it is approved. That leading
  paragraph (bold or plain, up to the first blank line) is now stripped at
  approval time (`tret/api/findings.py`'s approve path) and, defensively,
  again at export assembly (`tret/services/export.py::
  strip_draft_status_banner`) for any section that was approved before this
  fix shipped.
- **Importing the same connected file twice no longer creates two
  documents, and imports are now logged.** `POST /api/projects/{project_id}
  /documents/import` (`tret/api/documents.py`) previously created a document
  per item with no `connection_activity` row at all, and an identical
  duplicate document on every repeat of the same picked item. Each item that
  reaches the provider now logs one `action="import"` activity row (via the
  same `record_connection_activity` helper the connected-file read tools
  use), and an item whose bytes (`sha256`) and provider id
  (`meta.source.file_id`) match a document the project already has returns
  that existing document instead of a new one — the response shape is
  unchanged, with `"deduplicated": true` added on that one item. See
  docs/connections.md.

## Harness authoring now requires the admin or owner workspace role (2026-09-03)

Creating, updating or archiving a harness (`POST`/`PATCH`/`DELETE` under
`/api/harnesses`) now requires the `admin` or `owner` workspace role — the
same gate `pack_builder.py` already applied to packs. Reading harnesses
(`list`/`get`) is unaffected; every member can still see and run one.

This only changes behavior for a **self-host install running with
`TRET_MULTI_TENANT=false`** (the single shared-workspace mode). In that mode
a user's workspace role has always been their account role, so an `analyst`
or `approver` account that used to be able to create or edit harnesses will
now get `403 Forbidden` on those three endpoints. Multi-tenant deployments
are unaffected: every user is already `owner` of their own personal
workspace there.

If you run self-host with non-admin accounts that author harnesses, promote
them to `admin` (or `owner`) in that workspace:

```bash
curl -X PATCH "$TRET_URL/api/workspaces/<workspace_id>/members/<user_id>" \
  -H "Content-Type: application/json" \
  -H "Cookie: <your admin/owner session cookie>" \
  -d '{"role": "admin"}'
```

(the same `PATCH .../members/{user_id}` endpoint the frontend's member-role
picker uses) — or promote the account's global role at creation time via
`POST /api/auth/users`. No migration or restart is needed; the new gate takes
effect on the next request.

## Upgrading from bench (the rename to tret)

bench was renamed to **tret**. The schema is untouched — the rename runs no
migration — but four things outside the database move, and three of them fail
quietly if you skip them.

**1. Environment variables: `BENCH_*` → `TRET_*`.** Settings are read with the
`TRET_` prefix (`backend/tret/config.py`) and unrecognised keys are ignored, so
an old `.env` does not error. Every setting silently falls back to its shipped
default instead. Rename the keys in place:

```bash
sed -i.bak 's/^BENCH_/TRET_/' .env
```

This matters most for `TRET_SECRET_KEY`, which encrypts stored provider API
keys: booting with the shipped default instead of your old value leaves those
keys undecryptable and you have to re-enter them in Settings. Everyone is signed
out regardless, because the session cookie salt changed with the name. Under
`TRET_ENVIRONMENT=production` the boot checks catch a defaulted secret key and
refuse to start; in development they do not.

**2. Postgres role and database: `bench` → `tret`.** `docker-compose.yml` now
creates a `tret` role and a `tret` database. Postgres only runs its init step on
an empty data directory, so an existing `pgdata` volume still holds the old ones
and compose comes up pointing at a database that is not there. Either keep the
old names explicitly:

```bash
TRET_DATABASE_URL=postgresql+asyncpg://bench:bench@postgres:5432/bench
```

or rename them once and use the new default:

```bash
docker compose stop backend            # ALTER DATABASE needs no live connections
docker compose exec postgres psql -U bench -d postgres -c 'CREATE ROLE tmp_admin LOGIN SUPERUSER'
docker compose exec postgres psql -U tmp_admin -d postgres \
  -c 'ALTER DATABASE bench RENAME TO tret' \
  -c 'ALTER ROLE bench RENAME TO tret' \
  -c "ALTER ROLE tret PASSWORD 'tret'"
docker compose exec postgres psql -U tret -d postgres -c 'DROP ROLE tmp_admin'
```

The detour through `tmp_admin` is not optional: Postgres refuses to rename
the role you are connected as. The `ALTER ROLE ... PASSWORD` is not either —
a rename keeps the old password, and the compose default the backend
connects with is `tret`.

**3. Fly volume: `bench_storage` → `tret_storage`.** `fly.toml` mounts
`tret_storage`, and Fly will not rename a volume. Either set `source` back to
`bench_storage`, or create the new volume and copy `/data` across before
deploying. Skipping this deploys against an empty volume: uploaded documents
appear to vanish, though the old volume is still intact.

**4. Paths and scripts.** `start-bench.*` / `stop-bench.*` are now
`start-tret.*` / `stop-tret.*`, the Python package is `backend/tret/`, and the
CLI entry point is `tret` — re-run `pip install -e ".[dev]"` in `backend/` so
the stale `bench` console script is replaced. `install.sh` now defaults to
`~/kith-tret`; an existing `~/kith-bench` checkout keeps working, since nothing
reads the directory name.

## Upgrading from v0.1 (a database with no `alembic_version`)

v0.1 built its schema with SQLAlchemy's `create_all` at startup, which never
wrote an `alembic_version` row. Two things follow, and both are fixed:

- `create_all` adds missing **tables** but never adds a **column** to a table
  that already exists. On the first start of a newer release, a v0.1 database
  would crash with something like `column packs.content_hash does not exist`,
  and keep crashing — a restart loop.
- `alembic upgrade head` did not rescue it either: with no stamp, Alembic starts
  from base and fails on `CREATE TABLE users` because the table is already there.

Current releases detect this database and adopt it. The detection does **not**
assume a version — it inspects which tables and columns actually exist and
matches them against what each revision creates (`packs.content_hash`,
`runs.cache_read_tokens`, `runs.context_composition`, `runs.energy_wh`, and so
on). You will see, once:

```
WARNING [tret.schema] LEGACY DATABASE DETECTED: this database was created by an
  older tret release that built its schema directly from the models and left no
  Alembic stamp (15 tret tables, no alembic_version stamp; schema matches
  revision f18e4f74fc33). Inferred baseline revision f18e4f74fc33 by inspecting
  the columns that actually exist; stamping it and applying every migration after
  it. Nothing is dropped and no data is rewritten.
```

After that boot the database is stamped like any other and subsequent upgrades
are ordinary ones. **Back the database up first anyway** — it is a schema change
on data you care about, and one restore path is cheaper than none.

## <a id="a-database-no-revision-matches"></a>A database no revision matches

If a database has tret tables, no stamp, and a schema that matches no known
revision — half a migration applied by hand, a column dropped, a restore from
mismatched dumps — tret **fails to start** rather than stamping a guess. A wrong
stamp silently skips a migration, and the damage surfaces much later.

The error names every revision and what was missing from it:

```
tret found an existing database that no Alembic revision matches, and it has no
alembic_version stamp to go on. Refusing to guess a baseline, because stamping the
wrong one would skip a migration silently.
  Schema found (per revision, oldest first):
    c7ad56fb3365: fully present
    bcfa2baec8e2: fully present
    f18e4f74fc33: fully present
    a1c4e9d70b52: fully present
    d2b7f5a91c34: PARTIALLY present — missing runs.cache_write_tokens
    ...
```

To recover, decide the baseline yourself and tell Alembic:

```bash
cd backend
alembic history            # the chain, newest first
alembic stamp <revision>   # the last revision whose schema is fully present
alembic upgrade head
```

Pick the newest revision that is **fully** present. If a revision is partially
applied, finish or undo that column change by hand first (the migration files in
`backend/alembic/versions/` show exactly what each one does), then stamp it.

If the data is expendable, the shortest path is to drop the database and let
tret build it from scratch.

## Checking and driving the schema by hand

All of these run from `backend/` with `TRET_DATABASE_URL` set to the target
database. On Fly: `fly ssh console --app <app-name>` then `cd /app`.

```bash
alembic current              # the revision this database is stamped at
alembic heads                # the revision the code expects
alembic history              # every revision, newest first
alembic upgrade head         # apply outstanding migrations
alembic downgrade -1         # undo the most recent one
```

`alembic current` printing nothing means the database is unstamped: either empty,
or the legacy case above.

## Opting out of the automatic step

`TRET_SKIP_MIGRATIONS=true` skips it entirely, for operators who run migrations as
a separate deploy step (or from a job with elevated DDL rights). tret then
assumes the database is already at head and will fail on the first query that
needs a missing column, so pair it with `alembic upgrade head` in the deploy
pipeline.

Non-Postgres URLs (a sqlite dev database) skip migrations too and get their
tables straight from the models: the migrations are Postgres-flavoured (JSONB,
array columns, a GIN index). Supported deployments use Postgres.

## Downgrading tret

Every migration defines a `downgrade()`, and CI proves the whole chain runs both
ways. To roll back to an older tret release, downgrade to that release's head
*before* deploying the old code:

```bash
alembic downgrade <that release's revision>
```

Downgrades drop the columns their upgrade added, so the data in them is gone.
Take a backup first.

One downgrade needs more than a backup to be safe: `8eef9d61c7c4` (adds
`users.disabled`) drops that column without touching `password_hash`.
Deactivation keeps `password_hash` intact by design, so any account
deactivated since this revision was applied comes back silently active the
moment the column recording that fact is gone. Before downgrading past it,
record which accounts are currently disabled and re-deactivate them by
whatever mechanism the older release uses, immediately after.

## For contributors: never let the models drift from the migrations

The bug this page exists for started as a model change without a migration. Any
change to `backend/tret/db/models.py` needs one:

```bash
cd backend
alembic revision --autogenerate -m "what changed"
# read the generated file — autogenerate is a first draft, not an answer
```

Then add the new revision to `REVISION_MARKERS` in `backend/tret/db/migrate.py`,
naming a table or column that only that revision creates. A unit test fails if
you forget, because legacy-database detection can only recognise revisions it has
markers for.

CI enforces both halves against a real Postgres
(`backend/tests/test_migrations_postgres.py`): Alembic autogenerate must produce
an **empty** diff against `Base.metadata` after `upgrade head`, and an unstamped
`create_all` database must still be adoptable.
