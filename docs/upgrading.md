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
