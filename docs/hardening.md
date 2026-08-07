# Production Hardening

bench ships with development defaults so `docker compose up` works in one step.
Those defaults are unsafe on a network. This is the checklist for a real
deployment, and an honest account of what each control does and does not
guarantee.

## 1. Turn on the boot checks

```
BENCH_ENVIRONMENT=production
```

With `production` set, bench **refuses to start** if:

- `BENCH_SECRET_KEY` is still `dev-secret-change-me` (or blank). That key signs
  session cookies and encrypts provider API keys stored through the settings
  UI — a known key means forgeable sessions and readable credentials.
- `BENCH_ADMIN_PASSWORD` is still `bench-admin`.

It **warns loudly** (but starts) if `BENCH_COOKIE_SECURE` is false.

The check runs in `create_app()` (`bench/config.py::enforce_production_safety`),
before the socket is bound, so a misconfigured deploy fails fast instead of
serving.

```bash
BENCH_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

Rotating `BENCH_SECRET_KEY` invalidates every session **and** makes
DB-stored provider keys undecryptable — re-enter them in the settings UI after
a rotation.

## 2. TLS and cookies

- Terminate TLS in front of bench (reverse proxy, or the platform's edge).
- Set `BENCH_COOKIE_SECURE=true`. The session cookie is already `httponly` and
  `samesite=lax`; `secure` is the part that depends on your deployment.
- The admin user is created on first boot only. Change its password afterwards
  with `POST /api/auth/password` (your own account; the current password is
  required, and the check is rate limited so it cannot be used as an oracle).
  `BENCH_ADMIN_PASSWORD` is not a live credential store — it is read only when
  no users exist.

### Credential lifecycle and session revocation

Sessions are stateless signed cookies, so there is no session table to delete
rows from. Revocation is bound to the credential instead: every cookie carries a
short fingerprint of the password hash it was minted against, and a request whose
fingerprint no longer matches is refused (`bench/api/auth.py::credential_version`).
argon2 salts every hash, so **setting a password always invalidates every session
for that account**, immediately:

| Endpoint | Who | Effect |
| --- | --- | --- |
| `POST /api/auth/password` | the account holder, current password required | password changed; every other session for the account ends, the caller's own cookie is re-issued |
| `POST /api/auth/users/{id}/password` | admin | rotation after an incident, or restoring a deactivated account; ends every session that account holds |
| `POST /api/auth/users/{id}/deactivate` | admin | clears the credential entirely — no login, no valid session. The row is kept (approvals and runs point at it); reversible by setting a password. Refused for your own account, and for the last active admin |

Two consequences worth planning for:

- **Upgrading to this behaviour logs everyone out once.** Cookies minted before
  credential-bound sessions carried a bare user id and are refused rather than
  honoured — accepting the old shape would be a way around revocation. The cost
  is one re-login per user, once.
- Password change and deactivation are the revocation primitives. Rotating
  `BENCH_SECRET_KEY` also invalidates every session, but it is the blunt
  instrument: it makes DB-stored provider keys undecryptable too (§1).

## 3. Login rate limiting

The login endpoint applies an in-memory sliding window over
`BENCH_LOGIN_WINDOW_SECONDS` (default 300), and counts each failed attempt in
**two** buckets:

| Key | Limit | What it stops |
| --- | --- | --- |
| `src\|<peer>\|<email>` | `BENCH_LOGIN_MAX_ATTEMPTS` (default 10) | one host guessing at one account |
| `acct\|<email>` | 5x that (`ACCOUNT_BURST_MULTIPLE`) | the same account attacked from many source addresses |

Exceeding either yields `429` with `Retry-After`. A successful login clears both
windows for that account.

Why two: `request.client.host` is the *socket* peer, so behind the reverse proxy
this document requires, it is the proxy's address for every request and an
IP-keyed bucket silently collapses into one bucket for the whole internet. The
first key therefore **degrades into a per-account bucket behind a proxy**, which
is the guarantee actually worth keeping; the second is the ceiling the first
loses when an attacker has many addresses.

`X-Forwarded-For` is deliberately **not** consulted. It is client-settable, so
trusting it would let an attacker mint a fresh bucket per attempt by varying a
header — a limiter that can be stepped around is worse than one that is merely
uninformative, and bench has no trusted-proxy configuration with which to tell a
forged hop from a real one.

Limits: the counter is **per process** and in memory — it resets on restart and
does not coordinate across workers (bench pins `workers=1`). Neither key contains
anything the client controls except the email already being attacked, so a
lockout can only ever affect one account. For anything internet-facing, put a
proxy/WAF limit in front as well.

## 4. Deterministic methods: the actual sandbox

Pack methods (`bench/services/methods.py`) run as short-lived subprocesses:

| Control | Mechanism | Guarantee |
| --- | --- | --- |
| Interpreter isolation | `python -I` | No `PYTHON*` env, no user site-packages, cwd off `sys.path` |
| Secrets | empty `env`, `close_fds=True` | No API keys, no DB URL, no inherited handles |
| CPU | `RLIMIT_CPU` 30s | Hard stop on spin loops |
| Memory | `RLIMIT_AS` 768MB | Where supported (a no-op on macOS) |
| File handles | `RLIMIT_NOFILE` 64 | Where supported |
| Processes | `RLIMIT_NPROC` 16 | Where supported; blunts fork bombs |
| Wall clock | manifest `timeout_seconds` | Process killed on timeout |
| Output | 5MB / 2000 rows | Refused beyond the cap |
| Data access | inputs materialized by the runner | The method gets no DB handle |
| Integrity | pack content hash re-verified | A pack edited since install cannot run |
| Network | `unshare --net` on Linux | **Only** where available (see below) |

### Network isolation

`BENCH_METHODS_NETWORK_ISOLATION` (default true) wraps each method in
`unshare --net` when all of these hold: the host is Linux, the `unshare` binary
exists, and the process may create a network namespace (root, `CAP_SYS_ADMIN`,
or unprivileged user namespaces). bench probes this once and logs the result. On
a macOS development machine, or in a container without the capability, you get a
warning and **no network isolation** — methods could open sockets.

If you cannot grant the capability, isolate at the container/pod level instead
(no egress for the bench workload, or a network policy that only allows your LLM
provider).

### What is NOT isolated

**The filesystem.** A method runs as the bench user and can read anything that
user can read — `./storage` uploads, the pack tree, application code — and write
anywhere that user can write. There is no chroot, no mount namespace, no seccomp
filter.

The only real boundary is the one you put around bench: run it in a container or
VM with a minimal filesystem, a non-root user, a read-only root filesystem where
possible, and controlled egress.

## 5. The pack safety scan: a deterrent, not a boundary

Pack validation (`bench/packs/safety.py`, surfaced by `bench packs validate`,
`POST /api/packs/validate`, and the boot-time install) AST-scans every method
entrypoint and **fails the pack** on:

- network modules — `socket`, `ssl`, `http*`, `urllib`, `ftplib`, `smtplib`,
  `poplib`, `imaplib`, `xmlrpc`, `asyncio`, …
- process spawning — `subprocess`, `multiprocessing`, `pty`, `runpy`,
  `os.system`, `os.popen`, `os.exec*`, `os.spawn*`, `os.fork`
- FFI — `ctypes`, `cffi`
- dynamic import and dynamic code — `importlib`, `__import__`, `eval`, `exec`,
  `compile`, `pickle`/`marshal`
- namespace escapes — `__builtins__`, `__subclasses__`, `__globals__`, …

Violations are validation errors with `file:line`, so a pack that trips them is
never installed.

**This is not a security boundary.** Any determined author can defeat an AST
check (`getattr` chains, names built from strings, a C extension reached through
an allowed module, data-driven dispatch). It catches accidents — a method that
quietly fetches a URL and stops being reproducible — and raises the cost of
casual abuse. Treat installing a pack as deploying code you reviewed.

## 6. Pack integrity pinning

At install time bench hashes every entry in the pack directory (sorted by
relative path; regular files contribute their bytes, symlinks contribute their
target string, build artefacts excluded) and stores it on `packs.content_hash`.
Before a method executes, the hash is recomputed and compared; a mismatch fails
the run with a message naming the pack and both hashes, and records a failed
`method_runs` row.

Symlinks are pinned by target and never followed, so adding, removing or
re-pointing one changes the hash. What the pin cannot cover is the *content*
behind a link that leaves the pack directory — keep pack content inside the pack
(docs/pack-authoring.md).

This catches tampering and accidental drift by whoever can write to the pack
directory — it is **not** a signature, since the same person can reinstall to
re-pin. Signed packs remain future work.

Intentional edits — refresh the pin by reinstalling the pack:

- restart bench (boot runs the idempotent pack install), or
- `POST /api/packs/install {"path": "/path/to/pack"}` (admin), or
- inspect first: `bench packs hash /path/to/pack` prints the hash an install
  would store.

Packs installed before this feature have a null hash: bench logs a warning and
runs them, and the next install pins them.

## 7. Guardrail observability

`GET /api/analytics/guardrails?days=30[&project_id=…]` reports whether the
guardrails are firing: method run counts and failure rates by `method_slug`,
recent method errors, and per-harness structured-output validation failures
(including how many exhausted the repair budget). Validation failures are read
out of recent run transcripts, bounded to the last 500 runs.

A rising method failure rate often means a pack integrity or environment
problem; rising validation failures usually mean a doctrine/schema mismatch or a
model that keeps citing values it never retrieved.

The same response carries the window's **estimated energy** — total Wh and
derived gCO2e, plus a per-harness breakdown with Wh per run. Unlike validation
pressure, energy is a grouped `SUM` over every run in the window rather than the
bounded transcript scan, and it counts only runs that carry an estimate (runs
predating ecological accounting store null, and null is not zero). Carbon is
derived by applying the *currently configured* `BENCH_GRID_CO2E_G_PER_KWH` to
the summed energy; each run additionally records the factor in force when it ran,
and `energy_basis` in the response says which is which. All of it is estimated —
see [eco-accounting.md](eco-accounting.md).

## 8. Database and storage

- Give bench its own Postgres role. Alembic owns the schema and bench brings the
  database to head on boot, including adopting a pre-migrations (v0.1) database —
  see [upgrading.md](upgrading.md). That role therefore needs DDL rights on its
  own schema. To keep DDL out of the app's role instead, run
  `alembic upgrade head` from a deploy step under a privileged role and set
  `BENCH_SKIP_MIGRATIONS=true` on the app (documented in `.env.example` and
  passed through by `docker-compose.yml`).
- Back the database up **before** an upgrade that migrates it. A failed migration
  rolls back (Postgres DDL is transactional), but a downgrade discards the columns
  it removes.
- `BENCH_STORAGE_DIR` holds uploaded documents in the clear. Put it on an
  encrypted volume with restrictive permissions, and back it up with the DB —
  findings reference documents by id.
- bench makes no outbound calls except to the LLM providers you configure (plus
  the optional OpenRouter catalog fetch, `BENCH_OPENROUTER_CATALOG=false` to
  disable). No telemetry.

## Minimum production checklist

- [ ] `BENCH_ENVIRONMENT=production`
- [ ] unique `BENCH_SECRET_KEY`, strong `BENCH_ADMIN_PASSWORD`
- [ ] TLS in front, `BENCH_COOKIE_SECURE=true`
- [ ] container/VM with a non-root user and controlled egress —
      [Dockerfile.fly](../Dockerfile.fly) does this already (a fixed-uid `bench`
      user; `fly-entrypoint.sh` fixes ownership of the mounted volume at boot,
      then drops root before exec'ing uvicorn), so this is a check on your
      deployment only if you built your own image from `backend/Dockerfile`
      instead, which does not set one
- [ ] `unshare` available, or network policy denying method egress
- [ ] packs reviewed as code, installed from a path only operators can write
- [ ] DB and `storage/` backed up, and backed up again before an upgrade
      (bench migrates the schema itself on boot — [upgrading.md](upgrading.md))
- [ ] proxy/WAF rate limit in front of `/api/auth/login`
