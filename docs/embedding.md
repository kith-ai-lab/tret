# Embedding tret

The workbench is one way to use tret. The router and the cost/carbon
accounting behind it are two more: a Python SDK you import into your own
code, and a CLI you can shell out to. Neither needs Postgres, FastAPI, or a
running server — both route through the same `ModelRouter` the full app
uses, and both hand back a `Receipt`: what the call cost, in dollars and
estimated carbon, and how that compares to a frontier-model counterfactual.

If you want doctrine, approvals, multi-tool agent loops, or deliverable
export, that's the workbench (`docker compose up`, see the [README](../README.md)).
If you want "route this one task to a sensible model and tell me what it
cost," that's this page.

## Install

**tret is not on PyPI yet** — PyPI publication ships with the public launch.
Until then, install from a checkout:

```bash
git clone https://github.com/voiz-academy/tret
pip install -e tret/backend              # core: SDK + CLI, no server deps
pip install -e "tret/backend[server]"    # + FastAPI/Postgres/the full workbench
```

The core install (`pyproject.toml`'s base `dependencies`) pulls in the
provider clients, `pypdf`, and `python-docx` — enough for the SDK and for
`tret run --path` to extract PDFs and Word documents — and nothing that
needs a database. `[server]` adds FastAPI, SQLAlchemy, Alembic, and the rest
of the workbench.

## Configure a provider

Both surfaces route through the same provider keys the app uses, read from
the environment (or a `.env` file next to wherever you run Python):

| variable | provider |
|---|---|
| `TRET_ANTHROPIC_API_KEY` | Anthropic |
| `TRET_OPENROUTER_API_KEY` | OpenRouter (OpenAI, Google, Meta, DeepSeek, and more) |
| `TRET_MOONSHOT_API_KEY` | Kimi / Moonshot |
| `TRET_LOCAL_BASE_URL` | any OpenAI-compatible local server (Ollama, LM Studio, vLLM, `llama.cpp`) — see [local-models.md](local-models.md) |

At least one has to be set. With none configured there are no candidate
models to route to, and both the SDK and the CLI raise
`tret.router_llm.router.RoutingUnavailable` — the same exception the router
raises inside the full app — with the message `"No candidate models: check
provider API keys and the harness model policy."`

## The SDK

```python
from tret import Router

result = Router().run("Summarize this paragraph in one sentence: ...")
print(result.text)
print(result.receipt)
```

`.run()` is a sync wrapper — see below for the `async`/`await` form. `Router()`
takes the same policy knobs a harness's `model_policy` does:

```python
Router(
    objective="balanced",     # quality | balanced | token_conservation | eco
    max_cost_tier="premium",  # local | economy | standard | premium
    allowed=None,              # restrict routing to a list of model ids
    model=None,                 # pin a model id — bypasses routing entirely
    temperature=0.2,
)
```

`objective` and `max_cost_tier` are validated in the constructor, not on the
first call — an unknown value raises `ValueError` immediately, naming the
valid choices, rather than surfacing three calls later inside an `await`.

Two async methods do the work:

- **`await r.arun(task, *, system=None, max_tokens=4096)`** routes `task`,
  makes exactly one model call — no tools, no multi-turn loop — and returns
  a `RunResult(text, model, receipt, stop_reason)`.
- **`await r.aroute(task)`** routes without executing anything, returning
  the raw `RoutingDecision` — useful if you want to see (or log) what would
  be chosen before spending anything.

`r.run(...)` and `r.route(...)` are sync wrappers around the two async
methods (`asyncio.run` under the hood). Calling either from inside a running
event loop raises a `RuntimeError` telling you to use `await
Router().arun(...)` instead — a loud refusal, not a nested-event-loop trick,
so the sync path never silently deadlocks.

A `Router` instance builds and caches its catalog, provider registry, and
`ModelRouter` on first use, so reuse one instance across calls rather than
constructing a fresh `Router()` per task. It's not documented thread-safe —
fine for one router driving one event loop.

## The Receipt

Every `RunResult.receipt` (and every `LocalRunResult.receipt` from the CLI's
`arun`) is the same frozen dataclass:

| field | type | meaning |
|---|---|---|
| `model` | `str` | the tret model id that actually ran, e.g. `anthropic/claude-haiku-4-5` |
| `usd` | `float \| None` | this call's cost |
| `co2e_g` | `float \| None` | estimated grams CO₂e |
| `energy_wh` | `float \| None` | estimated watt-hours (compute only, no PUE) |
| `baseline_model` | `str \| None` | the frontier model this run is compared against |
| `avoided_usd` | `float \| None` | signed — see below |
| `avoided_usd_pct` | `float \| None` | signed percentage |
| `avoided_co2e_g` | `float \| None` | signed |
| `avoided_co2e_pct` | `float \| None` | signed percentage |
| `usage` | `dict` | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` |
| `routing` | `dict` | `chosen_model`, `reasoning`, `candidates`, `fallback_used`, `objective` |
| `overhead` | `dict \| None` | the router's own spend — see below |
| `raw` | `dict` | the complete `energy_accounting()` output, untouched |

**`None` always means "estimate unavailable" — never zero.** A `Receipt`
whose `usd`/`co2e_g`/`energy_wh` are all `None` is not free; it's one where
the provider never reported usable usage at all (no completed turn, or an
all-zero `Usage`) — some local OpenAI-compatible servers ignore
`stream_options.include_usage` entirely, and printing `$0.0000` for that
would be a fabricated number, not an estimate. `str(receipt)` reflects this:
a Receipt with no priceable usage prints as `receipt · estimate unavailable
· <model>` instead of a confident-looking `$0.0000`.

**The frontier-baseline counterfactual is a model-selection signal, not a
saving.** `avoided_usd`/`avoided_co2e_g` (and their `_pct` companions) are
what your run's exact token counts would have cost and emitted on tret's
curated frontier model, re-priced through *that* model's rates and energy
class — same tokens, different model. They are signed: a run heavier or
dearer than the baseline reports a **negative** figure rather than a
clamped zero, because a different model would not actually have produced
these token counts. Treat these numbers as "did routing away from the
frontier help, and by how much" — never as a booked saving, an offset, or
anything usable for statutory carbon reporting. `raw["baseline"]["basis"]`
and `raw["cost"]["basis"]` carry the full wording tret stores with every run.

**Router overhead is kept separate on purpose.** Choosing a model is itself
a model call (the router model reads the task and picks). That call is
accounted against *its own* model, not folded into `usd` — `overhead` is
`None` when no router call was made (a pinned model, or only one candidate
to begin with), and otherwise a dict with `kind`, `model`, `provider`,
token counts, `cost_usd`, `energy_wh`, and a full `energy_accounting` block
for that call alone. `str(receipt)` surfaces just the dollar figure, as
`... (+$0.0026 routing)`, so it's visible without doubling as part of the
headline cost.

`raw` is the complete dict `services/emissions.energy_accounting()`
returns for this call — the same shape a persisted run's
`runs.energy_accounting` carries, including per-factor provenance
(`raw["factors"]`), the honest caveat list (`raw["caveats"]`), and the
uncertainty band (`raw["uncertainty"]`). It's large (tens of fields); the
Receipt's own top-level fields above are the values most callers want.

## Pinning vs routing

Leave `model=None` (the default) and `Router`/`tret run` route
automatically: candidates are filtered by `allowed`, `max_cost_tier`, and
which providers have keys, then picked per `objective`. Set `model="<id>"`
(SDK) or `--model <id>` (CLI) and routing is bypassed entirely — the
`routing` dict on the resulting Receipt will show that one model as the
only candidate, with `fallback_used: false` and no router call, so no
`overhead`.

## `max_cost_tier="local"` as a confidentiality control

Tier order is `local < economy < standard < premium`; `max_cost_tier` only
ever restricts how expensive a chosen model can be. Because local inference
is zero-cost by definition, `max_cost_tier` can never exclude a local
model — which makes setting it *to* `"local"` a way to exclude every cloud
candidate instead: `Router(max_cost_tier="local")` or `tret run --max-cost-tier
local` means no cloud provider is ever a candidate for this call, full stop.
That's a confidentiality boundary, not a spending one — the same framing
[local-models.md](local-models.md) uses for a harness capped this way. A
call capped at `local` with no tool-capable local model configured fails
loudly (`RoutingUnavailable`) rather than quietly falling back to the cloud.

## The CLI

```
$ python -m tret.cli run --help
usage: tret run [-h] [--path PATH] [--out OUT] [--model MODEL]
                [--objective OBJECTIVE] [--max-cost-tier MAX_COST_TIER]
                [--max-iterations MAX_ITERATIONS] [--json] [--quiet]
                task

positional arguments:
  task                  The task to perform, in plain language

options:
  -h, --help            show this help message and exit
  --path PATH           Root directory of local files the model may read
  --out OUT             Write the final text here instead of stdout
  --model MODEL         Pin a specific model id, bypassing the router
  --objective OBJECTIVE
                        Routing objective
  --max-cost-tier MAX_COST_TIER
                        Cost ceiling tier (local | economy | standard |
                        premium)
  --max-iterations MAX_ITERATIONS
                        Cap on agent-loop iterations (at least 1)
  --json                Emit machine-readable JSON to stdout
  --quiet               Suppress progress lines on stderr
```

(installed as the `tret` console script — `tret run ...` — this is
`python -m tret.cli run ...` for a checkout without an activated install.)

With `--path DIR`, the routed model gets three read-only tools —
`list_files`, `read_file` (with PDF/`.docx` text extraction and
offset-based paging for long files), and `search_files` (case-insensitive
substring search) — every path confined under `DIR`: traversal (`../..`),
an absolute path, and a symlink that resolves outside `DIR` are all rejected
as a tool error, never a crash — `list_files` and `search_files` apply the
same check to every path they surface, so a symlink planted inside `DIR`
that points outside it is neither listed nor searched. Hidden files
(dotfiles, or anything under a dot-directory such as `.git/`) and
obviously-binary extensions are skipped by the listing and refused by
`read_file` alike. Without `--path`, no tools are offered and
the run behaves like a single `Router.arun()` call, plus the ledger entry.

### A real transcript

Below is real, unedited output — not reconstructed — from a checkout with
`TRET_OPENROUTER_API_KEY` configured, run against a two-line `notes.txt`:

```
$ tret run "Summarize the Q3 numbers in notes.txt in two sentences." --path ./q3-notes
→ routed to gemini-3.5-flash-lite · This is a very small, straightforward summarization
  task with a free-text output contract. Gemini 3.5 Flash Lite is explicitly optimized
  for cheap summaries and is the lowest-cost suitably capable option, while its large
  context is more than sufficient.
→ search_files query='Q3'
→ read_file path='notes.txt'
In Q3, revenue reached $482,000, representing a 12% year-over-year increase, while churn
decreased to 3.1% from 3.8% in Q2. Additionally, Northwind Traders became the largest
customer, accounting for 18% of total revenue.
receipt · $0.0006 (+$0.0026 routing) · 0.02 gCO₂e · gemini-3.5-flash-lite · ledger #5bcc
```

The `→` lines and the receipt line are progress output on stderr; the
answer is the only thing on stdout (or, with `--out FILE`, is written to
`FILE` instead and stdout stays empty). Since routing consults a live
router model, the exact model chosen and its stated reasoning can vary
between runs even for the same task — the shape above (a routing line, tool
calls as they happen, the answer, then a one-line receipt) is what's stable.
`--quiet` suppresses everything but the answer and any `--json` payload.

### `--json`

`--json` prints one JSON object to stdout instead: `text`, `receipt` (the
full dataclass, via `dataclasses.asdict`), `ledger_id`, `status`
(`"completed"`, `"hit_iteration_cap"`, or `"failed"`), `iterations`, and
`error` (the provider error message when status is `"failed"`, else
`null`). It's the same
receipt shape documented above — including the large `raw` block — so this
is what to parse if you're shelling out to `tret run` from another program
rather than importing the SDK directly.

## What `tret run` does not do

- **No network tools.** Only the three local-file tools above are ever
  offered — no `web_search`, no `fetch_url`, no calling out to anything but
  the routed model's own provider.
- **No packs.** No `pack.yaml`, no doctrine, no JSON-Schema-validated
  verdicts, no `lookup_dataset`/`run_method`. It's a plain freeform loop.
- **Read-only, path-confined file access.** `--path` grants reading, never
  writing, and only inside the directory you named.
- **No database, no approvals, no audit trail beyond the ledger.** There's
  no run row, no reviewer queue, nothing persisted except the one ledger
  line described below.
- **No mid-run model switching.** One model is chosen at the start of the
  run and used for every iteration — the workbench engine's supervisor
  (which can hand off to a different model mid-run) has no equivalent here.

## The ledger

Every `tret run` — with or without `--path`, whatever the outcome — appends
one line of JSON to a local ledger file: `~/.tret/ledger.jsonl` by default,
overridable with `TRET_LEDGER_PATH` (or `Settings.ledger_path`). A failed
write (unwritable directory, full disk) is logged to stderr and never fails
the run — the answer and receipt were already earned.

A run that does not complete is still accounted for. A provider error
mid-run ends the run with status `"failed"`; the tokens already spent are
priced, the receipt prints, and the ledger line records it. In both
non-completed cases (`"failed"`, `"hit_iteration_cap"`) `tret run` exits
nonzero, appends `· status <status>` to the receipt line, and refuses to
touch `--out` — an existing file is never overwritten with a partial or
empty answer. The ledger file is created `0600`: its lines carry your own
task text.

One real entry, from the run above:

```json
{"id": "5bccf44cae7f4b4d9442ca1d3a1ece7b", "ts": "2026-08-20T21:22:13.850174+00:00", "task": "Summarize the Q3 numbers in notes.txt in two sentences.", "model": "openrouter/google/gemini-3.5-flash-lite", "status": "completed", "iterations": 3, "usd": 0.0005907, "co2e_g": 0.022271, "energy_wh": 0.039488, "avoided_usd_pct": 96.372, "avoided_co2e_pct": 98.81, "overhead_usd": 0.002636, "out": null}
```

Field list:

| field | meaning |
|---|---|
| `id` | a `uuid4().hex`; `tret run` prints its first 4 characters as `ledger #<...>` |
| `ts` | UTC ISO-8601 timestamp |
| `task` | the task string, truncated to 200 characters |
| `model` | the tret model id that ran |
| `status` | `"completed"`, `"hit_iteration_cap"`, or `"failed"` |
| `iterations` | agent-loop iterations used |
| `usd`, `co2e_g`, `energy_wh` | from the run's `Receipt` — `None`/`null` under the same "unavailable, not zero" rule described above |
| `avoided_usd_pct`, `avoided_co2e_pct` | from the `Receipt`'s frontier-baseline comparison |
| `overhead_usd` | the router's own spend (`receipt.overhead["cost_usd"]`), or `null` when no router call was made |
| `out` | the `--out` path, if one was given, else `null` |

There's no ledger reader shipped yet — it's a flat, append-only,
`jq`-friendly file, not a database.
