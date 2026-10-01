# Changelog

All notable changes to tret are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor releases may
include breaking changes; [docs/upgrading.md](docs/upgrading.md) covers every
one that needs action on an existing install.

## [Unreleased]

### Added

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

### Fixed

- Saving the emissions settings form no longer deletes settings the form does
  not show (`energy_strategy`, `model_overrides`, `pue.upstreams`, grid rows for
  other providers or regions, the embodied GPU model). The form previously
  rebuilt the whole document on save.

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

[Unreleased]: https://github.com/kith-ai-lab/tret/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kith-ai-lab/tret/releases/tag/v0.1.0
