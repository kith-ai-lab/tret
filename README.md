# tret

**An open-source AI harness platform for non-technical knowledge work.**

tret is what a coding agent is for engineers, built instead for analysts:
a workbench where AI does rigorous, auditable knowledge work — structured
verdicts, evidence extraction, deliverable drafting — under rules that make
its output trustworthy enough to put in front of a client, a credit officer,
or an auditor.

The flagship domain pack is **climate risk assessment**; the core is
domain-agnostic and packs are pluggable.

## Why tret is different

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

One command on macOS or Linux — it checks for Docker, clones tret into
`~/kith-tret`, builds and starts it, and opens your browser:

```bash
curl -fsSL https://tret.kithailab.com/install.sh | bash
```

Re-run it later to update in place (it won't touch a checkout you've edited).
That URL serves [install.sh](install.sh) from this repo verbatim, so
`https://raw.githubusercontent.com/kith-ai-lab/tret/main/install.sh` is the
same script — read it before piping it to bash if you'd rather not do that
blind.

**Verify before running.** Checking the download against `tret.kithailab.com`
itself proves nothing — that's the same host that could serve you a bad file.
[install.sh.sha256](install.sh.sha256) is committed in this repo instead, so
fetch it from GitHub's raw content host and verify against that:

```bash
curl -fsSLO https://tret.kithailab.com/install.sh
curl -fsSLO https://raw.githubusercontent.com/kith-ai-lab/tret/main/install.sh.sha256
shasum -a 256 -c install.sh.sha256   # must print: install.sh: OK
bash install.sh
```

That `raw.githubusercontent.com` fetch only works once this repo is public —
today it's private, so the URL 404s. Until then, the clone path just below is
the way to actually inspect the script before running it; `install.sh.sha256`
still verifies once the repo goes public.

Or clone the repo and run the same script from your own checkout — git's own
integrity checking stands in for the checksum here:

```bash
git clone https://github.com/kith-ai-lab/tret.git && cd tret
less install.sh          # read it
./install.sh             # then run it
```

**Not a terminal person?** Double-click `start-tret.command` (macOS) or
`start-tret.bat` (Windows) and tret sets itself up — Docker check, first-run
config, browser open. [docs/easy-start.md](docs/easy-start.md) is the
plain-language walkthrough, `stop-tret` the off switch. Or skip installing
anything and [deploy to Render with one click](docs/deploy-render.md).

Already have a checkout, or want to drive it yourself:

```bash
cp .env.example .env       # defaults are fine — provider keys are added in-app
docker compose up --build
```

On first login tret asks for a provider key (one OpenRouter key alone works;
Anthropic and Moonshot too) and stores it encrypted — no config file editing.
Prefer no cloud at all? Set `TRET_LOCAL_BASE_URL` to your own inference server
and tret runs with no cloud provider — from the compose stack that is
`http://host.docker.internal:11434/v1` for a host Ollama, not `localhost`.

Open http://localhost:5180, log in (`admin@example.com` / `tret-admin` by
default — change in `.env`), and you're in a seeded demo:

- a **Chat** front door — just ask ("*Is the vendor flood score for Alder
  Point still trustworthy?*") and the assistant recognizes the request,
  triggers the right specialist harness task via its `run_harness_task` tool,
  and reports the draft verdict back — with the delegated run fully audited
  and its finding waiting in the approval queue. It can also fan several
  independent specialist tasks out at once (`delegate_parallel`) and brief
  short-lived, read-only subagents for a quick lookup (`spawn_subagent`).
  Every delegated run, however it was started, is a real Run with its own
  audit trail — none of this is a hidden side channel — and the turn's own
  cost cap covers the whole tree, not just the top-level run.

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

tret speaks to **Anthropic** (native SDK, prompt caching), **Kimi /
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

Set one variable — `TRET_LOCAL_BASE_URL` — and tret runs with no cloud
provider, no API key, and no egress past your own machine. Local models are
discovered from the server, **probed for real tool-calling support** (tret's
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

## One door to the internet

Everything tret sends outward goes through one module, `backend/tret/net/`,
tagged with a destination **class** — cloud providers, the model catalog, your
local model server, and *research* (web search and page fetch). Each class has
its own switch, every switch narrows and none widens, and a test fails the build
if any other module in the codebase so much as constructs an HTTP client. "What
can this deployment reach?" is one directory, not a grep.

Research ships **off**. It is the only class whose destination is chosen by a
model — from text that may have arrived in an uploaded document — so turning it
on is a deliberate act, and

```
TRET_EGRESS_RESEARCH=off
```

takes it away again without disturbing anything else. Harnesses that use the web
tools keep working: the tools stay registered, the engine withholds them, and the
run says so. An admin can cut any class from the running app (Settings → Network
access) and cannot restore one the environment took away.

When it is on, a fetched page does not become prose in a transcript. It is
stored byte-for-byte, hashed, and recorded as a document with its URL and fetch
time, so a reviewer can see the page **as it was when the run read it** — and it
still cannot supply a number to a verdict, because nothing fetched enters the
retrieved-values record the citation check reads. The web is a source you can
quote and attribute, not a third door for values.
[docs/hardening.md](docs/hardening.md) §9 has the rest, including the honest
account of what an app-level allowlist does and does not buy you.

## The ecological cost line

Every run carries an estimated **energy (Wh) and carbon (gCO₂e)** figure next to
its dollar cost — live per turn in the run view, in the run's audit record, and
in the provenance appendix of every exported deliverable. tret's flagship
domain is climate risk; a platform that produces TCFD sections cannot treat its
own footprint as somebody else's problem.

It is an estimate, and tret labels it one everywhere it prints it: a heuristic
energy class per model times weighted tokens (cached reads weighted lower,
because they are), converted with a grid intensity you set for your own region
(`TRET_GRID_CO2E_G_PER_KWH`). Nothing here is metered. What makes it worth
reporting anyway is that it is *actionable* — model choice moves the number by
more than an order of magnitude, and tret already picks the model. The `eco`
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
tret packs validate ./my-pack
```

Validation AST-scans every deterministic method and refuses the pack outright on
network access, process spawning, FFI, dynamic code, or namespace escapes — a
method that phones out is not reproducible. Installing pins a **content hash**
over every file in the pack, re-verified before any method runs, so editing pack
code under a running deployment fails loudly instead of silently changing
results. Both are deterrents against accidents and drift, not security
boundaries: installing a pack is deploying code you reviewed.

See [docs/pack-authoring.md](docs/pack-authoring.md).

## Embed it

The router and its cost/carbon accounting also work outside the workbench —
no Postgres, no FastAPI. From a checkout:

```bash
pip install -e backend  # core: SDK + CLI, no server deps (not on PyPI yet)
```

```python
from tret import Router
result = Router().run(task)
result.receipt  # $ and gCO2e, per call
```

```bash
$ tret run "Summarize the Q3 numbers in notes.txt in two sentences." --path ./q3-notes
→ routed to gemini-3.5-flash-lite · a small, cheap-summary-optimized model is sufficient...
In Q3, revenue reached $482,000, up 12% YoY, while churn fell to 3.1%.
receipt · $0.0006 (+$0.0026 routing) · 0.02 gCO₂e · gemini-3.5-flash-lite · ledger #5bcc
```

`tret run` adds read-only local-file tools (`--path`), an optional `--json`
payload, and an append-only `~/.tret/ledger.jsonl` receipt log — no packs, no
DB, no network beyond the routed model's own provider. See
[docs/embedding.md](docs/embedding.md).

## A tret for tret

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
unsafe on a network. Set `TRET_ENVIRONMENT=production` and tret **refuses to
boot** while the default secret key or admin password is still in place, rather
than serving insecurely. Also included: login rate limiting, subprocess
isolation for pack methods (rlimits, empty environment, and an empty network
namespace where the platform allows it), and an honest account of what is *not*
isolated.

The fastest path to a real instance is one click:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/kith-ai-lab/tret)

[render.yaml](render.yaml) provisions the app, a managed Postgres, and a
persistent disk, with production secrets generated at deploy time —
[docs/deploy-render.md](docs/deploy-render.md) walks through it (≈$13/mo).

[docs/hardening.md](docs/hardening.md) is the checklist;
[docs/deploy-fly.md](docs/deploy-fly.md) is a worked single-app deployment.

### Upgrading

Pull the new version and start it. tret migrates its own database on boot —
including a v0.1 database created before tret used migrations, which it detects
and adopts. Back up first, and read
[docs/upgrading.md](docs/upgrading.md) for what the startup log tells you, how to
check the current revision, and the manual recovery path.

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
| [connections.md](docs/connections.md) | Google Drive / Microsoft 365 workspace connections |
| [embedding.md](docs/embedding.md) | the SDK and the `tret run` CLI |
| [eco-accounting.md](docs/eco-accounting.md) | routing objectives, energy and carbon |
| [local-models.md](docs/local-models.md) | zero-cloud operation |
| [evals.md](docs/evals.md) | the golden-run suite |
| [hardening.md](docs/hardening.md) | production checklist |
| [telemetry.md](docs/telemetry.md) | the opt-in anonymous usage report: every field, on or off |
| [upgrading.md](docs/upgrading.md) | schema migrations, legacy databases, recovery |
| [deploy-fly.md](docs/deploy-fly.md) | reference deployment |
| [deploy-render.md](docs/deploy-render.md) | one-click hosted deployment |
| [easy-start.md](docs/easy-start.md) | run tret locally without a terminal |
| [demo-script.md](docs/demo-script.md) | a guided walkthrough of the seeded demo |

## Privacy

Configuration is env-only. tret makes no network calls except to the LLM
providers you configure (plus an optional OpenRouter model-catalog fetch you can
disable, and web research if you switch it on — off by default). With
`TRET_EGRESS=off` and a local model server it makes none at all. Every one of
those goes through a single module you can read in an afternoon. No telemetry
unless an admin turns it on.

tret can optionally report anonymous, aggregate usage statistics to Kith —
off by default, and an admin has to opt in before anything is sent.
[docs/telemetry.md](docs/telemetry.md) lists every field the report can ever
carry; nothing in it is a name, an id tied to you, or any piece of your data.

## Contributing

Domain packs, provider integrations, and hardening are the most valuable
contributions right now. See [CONTRIBUTING.md](CONTRIBUTING.md). Everyone
taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md). To report a
security issue, see [SECURITY.md](SECURITY.md) — please don't open a public
issue for it. Release notes live in [CHANGELOG.md](CHANGELOG.md).

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
