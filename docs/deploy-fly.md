# Deploying to Fly.io

bench runs as a **single Fly app**: the backend serves the built frontend
(`Dockerfile.fly`), talking to a Fly Postgres cluster, with a volume for
uploaded documents. One always-on machine — the run event bus is in-process,
so do **not** scale horizontally (see docs/architecture.md).

## One-time setup

```bash
fly apps create <app-name> --org <org>
fly postgres create --name <app-name>-db --org <org> --region <region> \
    --initial-cluster-size 1 --vm-size shared-cpu-1x --volume-size 3
fly postgres attach <app-name>-db --app <app-name>
fly volumes create bench_storage --app <app-name> --region <region> --size 3

fly secrets set --app <app-name> --stage \
  BENCH_DATABASE_URL="<the postgres:// URL printed by attach>" \
  BENCH_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  BENCH_ADMIN_EMAIL="you@example.com" \
  BENCH_ADMIN_PASSWORD="<strong password>" \
  BENCH_OPENROUTER_API_KEY="sk-or-..."        # and/or Anthropic/Moonshot keys
```

Notes:
- `attach` also sets a `DATABASE_URL` secret; bench reads `BENCH_DATABASE_URL`,
  so set it explicitly (same value). Plain `postgres://` URLs and libpq
  `sslmode` params are normalized automatically.
- `BENCH_ENVIRONMENT=production`, `BENCH_COOKIE_SECURE=true`, and the
  frontend/static serving are already set in `fly.toml` / `Dockerfile.fly`.

### Production mode is on, so the secrets above are not optional

`BENCH_ENVIRONMENT=production` makes bench **refuse to boot** while the shipped
development defaults are still in place — a dev `BENCH_SECRET_KEY` (forgeable
sessions, trivially decryptable stored provider keys) or the default
`BENCH_ADMIN_PASSWORD`. Set both in the `fly secrets set` above, before the
first deploy. A machine that fails this check logs the reason and exits:

```
Refusing to start with BENCH_ENVIRONMENT=production:
  - BENCH_SECRET_KEY is still the shipped default. ...
See docs/hardening.md.
```

Rotating `BENCH_SECRET_KEY` later invalidates sessions **and** every provider
key stored through the Settings UI (they are encrypted with it); re-enter those
keys afterwards. Full checklist: [hardening.md](hardening.md).

## Deploy

```bash
fly deploy --remote-only
```

On boot bench migrates its own schema to the current Alembic head, then runs the
idempotent seed (admin user, sample project, packs). Subsequent deploys keep all
data — including deploys that add columns, and an upgrade from a v0.1 database
that predates migrations entirely. Full details, the manual recovery commands,
and how to check the current revision: [upgrading.md](upgrading.md).

Watch the first boot after an upgrade (`fly logs --app <app-name>`): the schema
step logs what it decided before anything is served.

```
INFO  [bench.schema] schema state: stamped (alembic_version = f4c1d8ab26e7)
INFO  [bench.schema] schema is at revision f4c1d8ab26e7
```

A machine whose database cannot be matched to a known revision exits with an
explanation and the exact `alembic stamp` / `alembic upgrade head` commands to
run, rather than starting and failing later on a missing column. Only one machine
runs at a time here (the run event bus is in-process, so do not scale out), but
the migration step takes a Postgres advisory lock regardless, so an overlapping
old and new machine during a deploy cannot both migrate.

## After first boot

1. Log in as the admin at `https://<app-name>.fly.dev`.
2. **Settings → Team**: create accounts for teammates (analyst / approver /
   admin). Passwords are shown once at creation — send them over a secure
   channel; there is no email flow (yet).
3. Provider keys can also be managed per-workspace in Settings (encrypted at
   rest with `BENCH_SECRET_KEY`); env secrets always win.

## Operations

- Logs: `fly logs --app <app-name>`
- Console: `fly ssh console --app <app-name>`
- DB shell: `fly postgres connect --app <app-name>-db`
- Scale memory (keep count at 1): `fly scale memory 2048 --app <app-name>`
- The health check hits `/api/healthz`.

## Cost expectations

Smallest useful footprint (one shared-cpu-1x/1GB app machine, one
shared-cpu-1x/256MB single-node Postgres, 6GB volumes) lands in the
single-digit dollars per month range, plus your LLM provider usage.
