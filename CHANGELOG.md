# Changelog

All notable changes to tret are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor releases may
include breaking changes; [docs/upgrading.md](docs/upgrading.md) covers every
one that needs action on an existing install.

## [Unreleased]

### Added

- **Archive a team.** `POST /api/workspaces/{id}/archive` (owner only, team
  workspaces only, the workspace name typed back as confirmation) soft-deletes
  a workspace: `workspaces.archived_at` is set, pending invites are revoked,
  the workspace drops out of every membership list, switch and invite links
  to it refuse, and its name may be reused. Every row in it is kept.
  Settings gains an "Archive this team" panel for owners. Extensions may
  refuse through the workspace gate with action `"archive"`.
- **`users.email_verified`.** A real column replaces the "has a password
  hash" stopgap that gated pending-invite listing and invite redemption. It
  follows the IdP's `email_verified` claim on every OIDC login, is true for
  administrator-created password accounts, and the migration backfills true
  for existing password accounts.
- **Stop works on a wedged run.** The engine now knows which runs this
  process is executing. Cancelling a run that is queued or running with no
  live task behind it (crashed task, missed by the startup sweep) marks it
  failed after a 30-second grace period, ends any open event stream, and
  frees the conversation it was blocking. The cancel response reports
  `{ok, live, marked}`.
- **Error boundary.** A render error in one view shows "This page hit an
  error" with Try again and Reload, leaving the sidebar and workspace
  switcher usable, instead of blanking the whole app.

### Changed

- Energy figures are labelled by what they are: the headline **Energy** is
  facility energy (IT energy × PUE, the basis for the carbon figure); **IT
  energy (before PUE)** is shown as the secondary figure; a run still
  streaming shows **live estimate**, a finished run **final**. The Emissions
  dashboard uses the same labels. No computation or stored value changed.
- **OpenRouter requests are zero-data-retention by default.** With
  `TRET_OPENROUTER_PROVIDER_PREFS` unset or blank, every OpenRouter call now
  sends `provider: {"zdr": true, "data_collection": "deny", "ignore":
  ["moonshotai"]}`, and the deterministic fallback table no longer lists
  `openrouter/moonshotai/kimi-k3`. An explicit value replaces the default
  (`{}` sends nothing). Direct `kimi/*` models still run, on your own
  `TRET_MOONSHOT_API_KEY`. See [docs/hardening.md](docs/hardening.md) §10 and
  [docs/upgrading.md](docs/upgrading.md).

### Fixed

- The OAuth connection callback refuses a workspace that was archived after
  the authorize step.

## [0.2.0] — 2026-10-15

### Added

- **Verdicts you can read.** Approvals and the run page render a
  `divergence_verdict` as a card: verdict, reason code, confidence, the
  methodology note and the cited dataset values, with the raw payload behind
  a fold and every retrieved value behind another. The run page lists each
  finding a run recorded with its status. `record_verdict` now tells the
  model what it recorded and asks it to state the verdict in its answer.
- **One verdict per case.** A new draft `divergence_verdict` for the same
  site and peril supersedes the older draft in the same project (status
  `superseded`, cross-linked through `supersedes` / `superseded_by` on the
  finding). Approved and rejected findings are never touched. Verdicts of
  `insufficient_data` are stored with status `noted`: they stay on the run
  and in `GET /api/findings`, but leave the approval queue, since the data
  request filed with them is the actionable item. The portfolio divergence
  rollup skips superseded rows.
- **Chat stop and resume.** A Stop button cancels the reply in flight.
  `GET /api/chat/{id}` reports `active_run_id` (the newest live top-level run
  on the conversation, under an hour old); the page re-attaches to it after a
  reload instead of forgetting the run, and a second send while a reply is
  live is refused with 409 rather than starting a second paid run.
- **Invites that explain themselves.** `POST /api/auth/invites/{token}/accept`
  returns 200 and switches to the team when the caller already redeemed that
  invite (it used to say "invalid or expired"), 410 with a reason for a
  revoked or expired invite, and 404 only for an unknown token or someone
  else's invite. `GET /api/auth/invites/pending` lists the caller's own open
  invites, and the first-run screen offers to accept one, or to switch to a
  team the user already belongs to, before it offers credits.
- **Routing errors that say why.** "No candidate models" now reports how
  many catalog models were considered and why each was excluded (no provider
  key per provider, above the policy's cost tier, not in the allowed list,
  no tool support), and logs the same breakdown.

- **Water consumption.** Every new run records an estimated water figure next
  to its carbon: on-site cooling water (IT energy × site WUE, default 0.375
  L/kWh from LBNL 2024; 0 for local runs) plus power-generation water
  (facility energy × grid water factor, default 4.81 L/kWh, the WRI 2020 world
  average, hydro evaporation included). Consumption basis only, with a ÷3/×3
  judgment band. Stored as `energy_accounting.water`; returned by
  `GET /api/analytics/emissions`, the what-if endpoint and deliverable exports;
  configurable through a `water` block in the workspace emissions settings and
  `TRET_WATER_*` env vars; shown on the run page, the Emissions dashboard
  (Water view), the what-if drawer and the settings. Runs recorded earlier
  show "Not recorded", never 0. Method and sources:
  [docs/water-methodology.md](docs/water-methodology.md).
- **Per-upstream water disclosures.** A settings layer may carry
  `water.upstreams.<google|aws>` cooling-water figures, selected per call by
  the same `served_by` identity as per-upstream PUE. Entries must declare
  `water_basis: "consumption"` and `denominator: "it_energy"`, so a
  withdrawal-basis WUE cannot be entered as one. What-if scenarios that name no
  water keys now keep each run's recorded water factors.
- **Method v3 preview.** Every server-side run now also records a parallel
  emissions estimate under the revised `facility_v3` method (Kith method lab,
  2026-10-01), stored additively in `runs.routing["method_v3"]` and returned as
  a new optional `method_v3` field on run detail (`GET /api/runs/{id}`, slim;
  `?v3=full` for the whole block) and on the chat turn payload, null for runs
  that predate it. It is labelled a preview and never replaces `co2e_g`,
  `energy_accounting`, cost, analytics or exports, none of which change; the
  run `routing` field is returned without it. The run receipt shows it as a
  small secondary line.
- **New optional evidence keys.** Overhead (router / compaction) call records
  gain `served_by`, and per-call records in `model_timeline` gain
  `reasoning_requested`; both are additive and absent or null on older runs.
  Curated catalog entries may carry an `openrouter_id`.

- **TypeScript SDK (`@tret/sdk`, `sdk/typescript/`).** A zero-dependency
  client for a running tret server, for Node ≥ 18, Electron and the browser
  (ESM + CJS + types). It covers auth (bearer token, session cookie or none),
  harnesses, packs, documents, runs, findings and `version()`. `runs.events()`
  streams a run as a typed `AsyncIterable` of every event the engine
  publishes, with its own SSE parser over `fetch` that reconnects on its own.
  `runs.complete()` creates a run, streams it to the end and returns the run,
  its findings and a `Receipt` with the Python SDK's fields; `null` there
  means unavailable, never zero. Errors are typed: `TretAuthError`,
  `TretNotFound`, `TretRateLimited`, and `TretBudgetRefused` for gate
  refusals, including a run refused by the pre-run gate. Request/response
  types are generated from a committed OpenAPI snapshot, which CI regenerates
  (`backend/scripts/dump_openapi.py`, no database needed) and diffs. Not on
  npm yet; [docs/embedding.md](docs/embedding.md#typescript) and the package
  README cover installing it from a checkout. A reconnect always goes back to
  the stream before it reads the run record, so a run that finished while
  the connection was down still delivers its trailing events. A 2xx that is
  not JSON (an SPA shell) is a `TretError`, not a parse crash.
  `complete()` asks for up to 500 findings, the server's cap.
- **`GET /api/version`** returns `{version, git_sha}`. It is unauthenticated,
  like `/api/healthz`. The sha comes from `TRET_GIT_SHA` (set it when
  building an image), else from `git rev-parse HEAD`, else `null`. The `git`
  answer is used only when the repository's top level is the tret checkout
  itself, and it is read off the event loop, once per process.
- The OpenAPI schema now lists the two accepted `action` values on
  `POST /api/findings/{id}/approval`. The body (`{action, note}`) and its
  validation are unchanged.

### Changed

- **A session always has a workspace.** A browser session for a user with
  several memberships lands on a default workspace (the cookie's workspace if
  still a membership, else the most recently joined team, else the first
  membership) instead of a 409 that left every workspace call failing until
  the user picked one. Bearer (API) callers keep the explicit contract: a
  missing or wrong `X-Tret-Workspace` header is still a 409.
- Password login redeems pending invites the way OIDC login does.
- Creating a team whose name matches one the caller already owns is a 409.
- Switching, creating or joining a workspace updates the UI in place; it no
  longer needs a reload.
- Findings carry two new statuses, `noted` and `superseded`, next to `draft`,
  `approved` and `rejected`. Clients that filter by status should expect
  them; neither can be approved.

### Fixed

- Saving the emissions settings form no longer deletes settings the form does
  not show (`energy_strategy`, `model_overrides`, `pue.upstreams`, grid rows for
  other providers or regions, the embodied GPU model). The form previously
  rebuilt the whole document on save.
- Tenancy isolation tests now also cover run children, run events, method
  runs and deliverable exports.

## [0.1.0] — 2026-09-29 — first public release

tret, an open-source AI workbench: a harness platform for running LLM agents
against real work with an audit trail, approvals, and an honest cost line.

### Added

- **Workbench and chat.** Chat-first UI with runs, run audit, approvals,
  delegated sub-runs, and a harness builder; harnesses link one or more domain
  packs.
- **Multi-provider routing.** OpenRouter, Anthropic, OpenAI-compatible and
  local model servers behind one interface, with an LLM router, routing
  preview (model choice and cost range before a run starts), bounded
  exploration, prompt caching, and per-run budgets.
- **Delegation.** `delegate_parallel` and `spawn_subagent` tools with limits,
  retries, and cancellation, shown as child runs in the UI.
- **One door to the internet.** A single egress chokepoint (`TRET_EGRESS`),
  optional web research through a bundled SearXNG, and zero-cloud operation
  with a local model.
- **Ecological cost line.** Per-run energy and emissions estimates with a
  layered factor model (workspace overrides, grid zones, what-if recompute);
  methodology in [docs/emissions-methodology.md](docs/emissions-methodology.md).
- **Grounding check.** Chat replies flag numbers that don't trace to retrieved
  data or the conversation.
- **Domain packs.** Pack format, validator, archive import/export, an in-app
  pack builder, and a registry client; ships a climate-risk pack.
- **Connections.** Microsoft 365 and Google Drive connectors
  ([docs/connections.md](docs/connections.md)).
- **Security and operations.** OIDC and password login, workspaces and roles,
  production boot checks, security headers, single-instance lock, non-root
  container image, and a hardening guide ([docs/hardening.md](docs/hardening.md)).
- **Install and deploy.** One-command installer, Docker Compose stack, and
  templates for Fly.io and Render.
- **Telemetry, off by default.** Optional anonymous aggregate usage reports
  that an admin must opt in to ([docs/telemetry.md](docs/telemetry.md)).
- **Approve all sections.** Deliverables, and the Approvals page for a drafted
  section, offer "Approve all N draft sections" for one deliverable after an
  inline confirm. Each section still gets its own named approval, tagged as
  coming from the batch action. Only each section's latest draft is approved,
  and a section whose draft changed after you confirmed is skipped.
- **Pending counts in the sidebar.** Approvals shows how many findings are
  waiting for a decision, and Deliverables how many deliverables have sections
  still awaiting approval. Both refresh every 30 seconds and immediately after
  a decision.

[Unreleased]: https://github.com/kith-ai-lab/tret/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kith-ai-lab/tret/releases/tag/v0.2.0
[0.1.0]: https://github.com/kith-ai-lab/tret/releases/tag/v0.1.0
