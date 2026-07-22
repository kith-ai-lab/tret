# bench

**An open-source AI harness platform for non-technical knowledge work.**

bench is what a coding agent is for engineers, built instead for analysts:
a workbench where AI does rigorous, auditable knowledge work — structured
verdicts, evidence extraction, deliverable drafting — under rules that make
its output trustworthy enough to put in front of a client, a credit officer,
or an auditor.

The flagship domain pack is **climate risk assessment**; the core is
domain-agnostic and packs are pluggable.

## Why bench is different

Five rules are built into the architecture, not just the prompts:

1. **The AI never invents numbers.** Numeric values enter a run only through
   the `lookup_dataset` tool, and every cited value is mechanically
   cross-checked against what was actually retrieved. An un-retrieved number
   is a validation error, not a hallucination that slipped through.
2. **Outputs are drafts until a named human approves.** The approver is
   stamped from the login session — there is no API field to claim approval.
3. **Every run is fully auditable**: which model ran, why the router picked it
   (verbatim reasoning, candidates considered, prompt version), the doctrine
   version (content hash), every tool call, tokens, and cost.
4. **Doctrine-as-context.** The reasoning rules live in versioned markdown
   files the agent must follow and cite by heading.
5. **Honest uncertainty.** `insufficient_data` is a respectable verdict;
   missing data becomes an explicit data request, never a guess.

## Multi-provider, with an LLM router

bench speaks to **Anthropic** (native SDK, prompt caching), **Kimi /
Moonshot**, and **OpenRouter** (which fronts OpenAI, Google, Meta, DeepSeek,
and open models). A harness can pin a model — or set `auto`, where a small,
fast router model reads the task shape and picks the best model within your
cost tier. The routing decision is persisted on every run and shown in the UI;
if the router is down, a deterministic fallback table takes over (and says so).

## Quickstart

```bash
cp .env.example .env       # add at least one provider key (OpenRouter alone works)
docker compose up --build
```

Open http://localhost:5180, log in (`admin@example.com` / `bench-admin` by
default — change in `.env`), and you're in a seeded demo:

- a **Chat** front door — just ask ("*Is the vendor flood score for Alder
  Point still trustworthy?*") and the assistant recognizes the request,
  triggers the right specialist harness task via its `run_harness_task` tool,
  and reports the draft verdict back — with the delegated run fully audited
  and its finding waiting in the approval queue

- the **climate-risk** pack installed, with sample sites, forward-looking
  regional signals, and vendor-style hazard scores
- a **Climate Analyst** harness — run a *Signal divergence assessment* for
  site `S-003` × `flood` from the Workbench and watch it retrieve data,
  reason under the doctrine, and record a verdict for approval
- a **General Assistant** harness for freeform document work

## Domain packs

A pack is a directory: `pack.yaml` (task types, tools, datasets), versioned
`doctrine/*.md`, JSON Schemas for structured outputs, deliverable templates,
and sample data. Validate yours with:

```bash
bench packs validate ./my-pack
```

See [docs/pack-authoring.md](docs/pack-authoring.md).

## Architecture

FastAPI + Postgres backend, React frontend, provider-neutral agent engine.
See [docs/architecture.md](docs/architecture.md) and
[docs/trust-doctrine.md](docs/trust-doctrine.md).

## Privacy

Configuration is env-only. bench makes no network calls except to the LLM
providers you configure (plus an optional OpenRouter model-catalog fetch you
can disable). No telemetry, ever.

## License

Apache-2.0.
