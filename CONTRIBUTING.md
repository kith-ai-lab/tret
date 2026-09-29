# Contributing to tret

Thanks for your interest! tret is early — the most valuable contributions
right now are **domain packs**, provider integrations, and hardening.

Not sure where to start, or want to talk through a pack before writing it?
Ask in `#contributors` or `#domain-packs` on the
[tret Discord](https://discord.gg/XsEr3wmDUA).

## Development setup

```bash
# Postgres
docker run -d --name tret-pg -e POSTGRES_USER=tret -e POSTGRES_PASSWORD=tret \
  -e POSTGRES_DB=tret -p 5432:5432 postgres:16-alpine

# Backend
cd backend
python3.12 -m venv .venv
# Always install with -c constraints.txt — it pins every dependency (direct
# and transitive) to the versions CI is known to pass against. A loose
# `pyproject.toml` floor (e.g. some `package>=X`) is free to resolve to a
# much newer release without it, and that drift is exactly what breaks tests
# locally while CI stays green (or the reverse) — `pip install -e ".[dev]"`
# on its own is the mistake this exists to prevent, not a fine shortcut:
.venv/bin/pip install -e ".[dev]" -c constraints.txt
TRET_PACKS_DIR=../packs .venv/bin/uvicorn tret.main:app --reload
# There is no `make install` — the Makefile only runs an already-set-up venv
# (`make backend`/`test`/`lint`), so this pip install is the one place the
# constraint file has to be named explicitly.

# Frontend (proxies /api to :8000)
cd frontend && npm install && npm run dev
```

## Before you open a PR

```bash
cd backend && .venv/bin/ruff check tret tests && .venv/bin/pytest -q
cd frontend && npm run build
```

CI runs exactly this, plus `tret packs validate` on the shipped pack, a
`docker compose` boot smoke test, and the schema-lifecycle job below.

## Changing the database schema

The ordinary suite runs on sqlite and in-process fakes, so it cannot see whether
a schema change reaches a real database. **Every change to
`backend/tret/db/models.py` needs a migration in the same PR:**

```bash
cd backend
alembic revision --autogenerate -m "what changed"   # then read the generated file
```

Then add the new revision to `REVISION_MARKERS` in `backend/tret/db/migrate.py`,
naming a table or column only that revision creates — that table is how tret
recognises a pre-migrations database and decides which revision to stamp it at
(docs/upgrading.md). `tests/test_schema_migrations.py` fails if you skip it.

Run the real-Postgres suite against a throwaway server before you push. It
creates and drops a database per test, and asserts the thing the sqlite suite
cannot: that Alembic autogenerate produces an **empty** diff against
`Base.metadata`, i.e. the models and the migrations have not drifted apart.

```bash
cd backend
TRET_TEST_POSTGRES_URL=postgresql+asyncpg://tret:tret@localhost:5432/postgres \
  .venv/bin/python -m pytest tests/test_migrations_postgres.py -q
```

CI runs it in the `migrations` job. Without `TRET_TEST_POSTGRES_URL` the module
skips, which is why `pytest -q` alone is not enough for a schema change.

## The golden-run policy

`backend/tests/evals/` holds the **golden runs**: scenarios that drive the real
engine, the real pack, real doctrine, real tools, and real validation against a
scripted model, each locking in one of the trust guarantees from the README. They
are the regression gate for the failure mode that has no stack trace — output
that still looks fine and is no longer grounded.

```bash
cd backend && .venv/bin/python -m pytest tests/evals -q     # offline, deterministic
```

They also run in the ordinary suite, so `pytest -q` already covers them.

**Any change to prompts, doctrine, routing, providers, or packs must keep them
green.** When one fails, exactly one of two things is true:

- **It is a regression** — fix the change.
- **The change is intentional and the expectation is now wrong** (a deliberately
  reworded preamble, a task's tool list changed on purpose). Update the
  expectation **in the same commit as the change**, and say why in the commit
  message. A golden expectation updated in a separate "fix tests" commit is
  indistinguishable from a regression that was papered over.

Never make a golden run pass by loosening it — dropping the exact failure text,
removing an assertion, widening a set. Loosening an eval is a change to what
tret promises, and it needs to be argued as one.

Adding a guarantee? Add the scenario in the same PR. Details, including the
`ReplayProvider` script format and the known gaps:
[docs/evals.md](docs/evals.md).

## Ground rules

- **The trust doctrine is not negotiable** (docs/trust-doctrine.md). PRs that
  let the model bypass dataset-only numbers, self-approve findings, or skip
  provenance will be declined regardless of how convenient they are.
- New providers implement `tret/providers/base.py`, add **one row** to
  `PROVIDER_SPECS` in `tret/providers/catalog.py`, and add curated entries to
  `models.yaml` with honest prices and strengths. That row is the single source:
  the registry builds from it, `GET /api/settings/providers` derives its list and
  env-key map from it, and the settings UI renders that response — so there is no
  fourth place to remember.
- New packs must pass `tret packs validate` and ship enough fictional
  sample data to demo every task type. No real client data, ever.
- Keep the single-worker event-bus constraint in mind (docs/architecture.md)
  until the LISTEN/NOTIFY bus lands.
