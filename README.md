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

1. **The AI never invents numbers.** Values enter a run only by retrieving
   dataset rows (`lookup_dataset`) or by invoking **vetted deterministic
   methods** (`run_method`) — pack-authored Python the agent can parameterize
   but never write, each execution manifest-pinned (params, code hash, output
   hash). Every cited value is mechanically cross-checked against what was
   actually retrieved or computed; anything else is a validation error, not a
   hallucination that slipped through.
2. **Outputs are drafts until a named human approves.** The approver is
   stamped from the login session — there is no API field to claim approval.
3. **Every run is fully auditable**: which model ran, why the router picked it
   (verbatim reasoning, candidates considered, prompt version, and which
   objective was in force), the doctrine version (content hash), every tool
   call, the token breakdown of the prompt itself, cost — and the estimated
   energy and carbon the run drew.
4. **Doctrine-as-context.** The reasoning rules live in versioned markdown
   files the agent must follow and cite by heading.
5. **Honest uncertainty.** `insufficient_data` is a respectable verdict;
   missing data becomes an explicit data request, never a guess. A run that
   never lands a valid verdict ends `completed_without_output` — a status that
   admits there is nothing to read, instead of one that implies there is.

## Quickstart

```bash
cp .env.example .env       # add at least one provider key (OpenRouter alone works)
docker compose up --build
```

No key at hand? Set `BENCH_LOCAL_BASE_URL` to your own inference server instead
and bench runs with no cloud provider at all — from the compose stack that is
`http://host.docker.internal:11434/v1` for a host Ollama, not `localhost`.

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
  reason under the doctrine, and record a verdict for approval — then read what
  the run cost, in dollars and in watt-hours
- a **General Assistant** harness for freeform document work
- **document outputs**: approved deliverable sections assemble into
  Markdown, HTML, or a styled **PDF** (with a provenance appendix — models,
  doctrine hash, approval status per section, estimated footprint) from the
  Deliverables view

A guided walkthrough with the seeded cases: [docs/demo-script.md](docs/demo-script.md).

## Multi-provider, with an LLM router

bench speaks to **Anthropic** (native SDK, prompt caching), **Kimi /
Moonshot**, **OpenRouter** (which fronts OpenAI, Google, Meta, DeepSeek, and
open models), and **any local OpenAI-compatible server** — Ollama, LM Studio,
vLLM, `llama.cpp`. A harness can pin a model — or set `auto`, where a small,
fast router model reads the task shape and picks the best model within your
cost tier. The routing decision is persisted on every run and shown in the UI;
if the router is down, a deterministic fallback table takes over (and says so).

**Routing objectives.** What "best" means is the operator's call, set per
harness and threaded through candidate ordering, the router prompt, the
deterministic fallback, *and* the persisted decision — so a preference is real
rather than advisory:

| objective | optimizes for |
|---|---|
| `quality` | the most capable candidate within the cost tier |
| `balanced` (default) | the cheapest model that will do the job well |
| `token_conservation` | small-but-sufficient models with disciplined output |
| `eco` | the least estimated energy per unit of work |

An unrecognized objective is a 422, never a silent fall back to the default.
See [docs/eco-accounting.md](docs/eco-accounting.md).

## Zero-cloud operation

Set one variable — `BENCH_LOCAL_BASE_URL` — and bench runs with no cloud
provider, no API key, and no egress past your own machine. Local models are
discovered from the server, **probed for real tool-calling support** (bench's
trust model is almost entirely forced tool calls, so a model that fakes them is
worse than useless), and priced at zero in a `local` cost tier. With no cloud
keys configured at all, routing still works: the LLM router step is skipped and
the deterministic fallback picks the local model.

A single harness can be pinned to local-only with
`max_cost_tier: "local"` — useful for confidential material on a deployment
that otherwise has cloud keys. If no tool-capable local model is available, that
harness fails loudly rather than quietly reaching for the cloud.

Never installed a model server? [docs/local-models.md](docs/local-models.md)
walks through Ollama from zero, and `docker compose --profile local up` bundles
one (first start downloads multi-GB weights, and CPU-only inference in a
container is slow — a native install is faster, especially on Apple Silicon).

## The ecological cost line

Every run carries an estimated **energy (Wh) and carbon (gCO₂e)** figure next to
its dollar cost — live per turn in the run view, in the run's audit record, and
in the provenance appendix of every exported deliverable. bench's flagship
domain is climate risk; a platform that produces TCFD sections cannot treat its
own footprint as somebody else's problem.

It is an estimate, and bench labels it one everywhere it prints it: a heuristic
energy class per model times weighted tokens (cached reads weighted lower,
because they are), converted with a grid intensity you set for your own region
(`BENCH_GRID_CO2E_G_PER_KWH`). Nothing here is metered. What makes it worth
reporting anyway is that it is *actionable* — model choice moves the number by
more than an order of magnitude, and bench already picks the model. The `eco`
objective is that knob. `GET /api/analytics/guardrails` rolls the figures up per
harness over a window.

Full derivation, and an honest account of the error bars:
[docs/eco-accounting.md](docs/eco-accounting.md).

## Token conservation

Doctrine and schemas are re-sent on every iteration of every run, so context
discipline is an architectural concern, not a tuning tip:

- **Prompt caching** on the Anthropic adapter, with cache reads and writes
  priced separately and reported per run — cheap reuse should be visible as
  such, not averaged away.
- **Context composition accounting**: the assembled prompt is broken down by
  component (preamble, each doctrine file, task instructions, output schema,
  tool specs) with estimated tokens for each, so you can see which part of your
  prompt is spending the money.
- **Task-scoped doctrine**: a pack task type declares the doctrine files — or
  individual sections — it actually needs. Unresolvable selectors fail *open*
  and are noted in the run's composition, so a scoping mistake can never starve
  a task of its rules.
- **Tool-result caps** with explicit `[TRUNCATED: …]` markers. Rows the model
  never saw are not citable, and the citation cross-check knows it.
- An optional per-run output-token budget that asks for a finalize once, then
  hard-stops a model that ignores it.

## Guardrail analytics

`GET /api/analytics/guardrails` answers whether the trust machinery is actually
firing: deterministic-method run counts and failure rates by method, recent
method errors, per-harness structured-output validation failures (including how
many exhausted their repair budget), and the window's estimated energy per
harness. A rising method failure rate usually means a pack-integrity or
environment problem; rising validation failures usually mean a doctrine/schema
mismatch, or a model that keeps citing values it never retrieved.

## Domain packs

A pack is a directory: `pack.yaml` (task types, tools, datasets), versioned
`doctrine/*.md`, JSON Schemas for structured outputs, deliverable templates,
and sample data. No backend code. Validate yours with:

```bash
bench packs validate ./my-pack
```

Validation AST-scans every deterministic method and refuses the pack outright on
network access, process spawning, FFI, dynamic code, or namespace escapes — a
method that phones out is not reproducible. Installing pins a **content hash**
over every file in the pack, re-verified before any method runs, so editing pack
code under a running deployment fails loudly instead of silently changing
results. Both are deterrents against accidents and drift, not security
boundaries: installing a pack is deploying code you reviewed.

See [docs/pack-authoring.md](docs/pack-authoring.md).

## A bench for bench

The guarantees above are only worth something if they cannot quietly stop being
true. Prompts get reworded, doctrine gets edited, a router change picks a
different model — none of that shows up as a stack trace. It shows up as output
that looks fine and is no longer grounded.

`backend/tests/evals/` is the regression gate for that class of change: **golden
runs** that drive the real engine, real pack, real doctrine, real tools, and real
validation against a scripted model, each locking in one of the five rules — a
hallucinated number is a validation error and not a finding; `insufficient_data`
is a first-class outcome; a vetted method's output is citable and the same number
without a method run behind it is not; a run that lands no valid verdict says so
in its status. They run offline and deterministically as part of the normal test
suite, and the evals police themselves (a broken script fails loudly instead of
passing vacuously).

Prompt, doctrine, routing, provider, and pack changes must keep them green.
See [docs/evals.md](docs/evals.md).

## Running it for real

The shipped defaults exist so `docker compose up` works in one step; they are
unsafe on a network. Set `BENCH_ENVIRONMENT=production` and bench **refuses to
boot** while the default secret key or admin password is still in place, rather
than serving insecurely. Also included: login rate limiting, subprocess
isolation for pack methods (rlimits, empty environment, and an empty network
namespace where the platform allows it), and an honest account of what is *not*
isolated.

[docs/hardening.md](docs/hardening.md) is the checklist;
[docs/deploy-fly.md](docs/deploy-fly.md) is a worked single-app deployment.

## Architecture

FastAPI + Postgres backend, React frontend, provider-neutral agent engine.
See [docs/architecture.md](docs/architecture.md) and
[docs/trust-doctrine.md](docs/trust-doctrine.md).

## Docs

| | |
|---|---|
| [architecture.md](docs/architecture.md) | how the pieces fit |
| [trust-doctrine.md](docs/trust-doctrine.md) | the rules, and why they are structural |
| [pack-authoring.md](docs/pack-authoring.md) | write a domain pack |
| [eco-accounting.md](docs/eco-accounting.md) | routing objectives, energy and carbon |
| [local-models.md](docs/local-models.md) | zero-cloud operation |
| [evals.md](docs/evals.md) | the golden-run suite |
| [hardening.md](docs/hardening.md) | production checklist |
| [deploy-fly.md](docs/deploy-fly.md) | reference deployment |
| [demo-script.md](docs/demo-script.md) | a guided walkthrough of the seeded demo |

## Privacy

Configuration is env-only. bench makes no network calls except to the LLM
providers you configure (plus an optional OpenRouter model-catalog fetch you
can disable) — or none at all, if you run local models. No telemetry, ever.

## Contributing

Domain packs, provider integrations, and hardening are the most valuable
contributions right now. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0.
