# Changelog

All notable changes to tret are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor releases may
include breaking changes; [docs/upgrading.md](docs/upgrading.md) covers every
one that needs action on an existing install.

## [Unreleased]

### Added

- **Approve all sections.** Deliverables, and the Approvals page for a drafted
  section, offer "Approve all N draft sections" for one deliverable after an
  inline confirm. Each section still gets its own named approval, tagged as
  coming from the batch action. Only each section's latest draft is approved,
  and a section whose draft changed after you confirmed is skipped.
- **Pending counts in the sidebar.** Approvals shows how many findings are
  waiting for a decision, and Deliverables how many deliverables have sections
  still awaiting approval. Both refresh every 30 seconds and immediately after
  a decision.

## [0.1.0] — first public release

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

[Unreleased]: https://github.com/kith-ai-lab/tret/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kith-ai-lab/tret/releases/tag/v0.1.0
