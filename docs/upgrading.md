# Upgrading bench

Short version: **pull the new version and start it.** bench migrates its own
database on boot, including adopting a database created by a release that did not
yet use migrations. Nothing to run by hand, and nothing is dropped.

The rest of this page is what happens under that sentence, and what to do in the
one case bench refuses to act on its own.

## What happens automatically

On every startup, before serving a single request, bench brings the schema to the
current Alembic head (`backend/bench/db/migrate.py`). It recognises three states:

| State | What bench sees | What it does |
| --- | --- | --- |
| **Empty** | no bench tables | `alembic upgrade head` — builds the whole schema from the first revision |
| **Stamped** | `alembic_version` holds a revision | `alembic upgrade head` — applies whatever is outstanding, a no-op when current |
| **Legacy** | bench tables present, `alembic_version` absent | infers the baseline revision from the schema, `alembic stamp`s it, then upgrades |

The step runs under a Postgres advisory lock, so two instances started together
serialise: one migrates, the others wait and then find the work done. Migrations
apply in a single transaction — Postgres DDL is transactional, so a failure part
way leaves the database exactly as it was.

Startup logs say what was decided:

```
INFO  [bench.schema] schema state: stamped (alembic_version = f4c1d8ab26e7)
INFO  [bench.schema] schema is at revision f4c1d8ab26e7
```

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
WARNING [bench.schema] LEGACY DATABASE DETECTED: this database was created by an
  older bench release that built its schema directly from the models and left no
  Alembic stamp (15 bench tables, no alembic_version stamp; schema matches
  revision f18e4f74fc33). Inferred baseline revision f18e4f74fc33 by inspecting
  the columns that actually exist; stamping it and applying every migration after
  it. Nothing is dropped and no data is rewritten.
```

After that boot the database is stamped like any other and subsequent upgrades
are ordinary ones. **Back the database up first anyway** — it is a schema change
on data you care about, and one restore path is cheaper than none.

## <a id="a-database-no-revision-matches"></a>A database no revision matches

If a database has bench tables, no stamp, and a schema that matches no known
revision — half a migration applied by hand, a column dropped, a restore from
mismatched dumps — bench **fails to start** rather than stamping a guess. A wrong
stamp silently skips a migration, and the damage surfaces much later.

The error names every revision and what was missing from it:

```
bench found an existing database that no Alembic revision matches, and it has no
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
bench build it from scratch.

## Checking and driving the schema by hand

All of these run from `backend/` with `BENCH_DATABASE_URL` set to the target
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

`BENCH_SKIP_MIGRATIONS=1` skips it entirely, for operators who run migrations as
a separate deploy step (or from a job with elevated DDL rights). bench then
assumes the database is already at head and will fail on the first query that
needs a missing column, so pair it with `alembic upgrade head` in the deploy
pipeline.

Non-Postgres URLs (a sqlite dev database) skip migrations too and get their
tables straight from the models: the migrations are Postgres-flavoured (JSONB,
array columns, a GIN index). Supported deployments use Postgres.

## Downgrading bench

Every migration defines a `downgrade()`, and CI proves the whole chain runs both
ways. To roll back to an older bench release, downgrade to that release's head
*before* deploying the old code:

```bash
alembic downgrade <that release's revision>
```

Downgrades drop the columns their upgrade added, so the data in them is gone.
Take a backup first.

## For contributors: never let the models drift from the migrations

The bug this page exists for started as a model change without a migration. Any
change to `backend/bench/db/models.py` needs one:

```bash
cd backend
alembic revision --autogenerate -m "what changed"
# read the generated file — autogenerate is a first draft, not an answer
```

Then add the new revision to `REVISION_MARKERS` in `backend/bench/db/migrate.py`,
naming a table or column that only that revision creates. A unit test fails if
you forget, because legacy-database detection can only recognise revisions it has
markers for.

CI enforces both halves against a real Postgres
(`backend/tests/test_migrations_postgres.py`): Alembic autogenerate must produce
an **empty** diff against `Base.metadata` after `upgrade head`, and an unstamped
`create_all` database must still be adoptable.
