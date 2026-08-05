# Running bench on local models

bench can run entirely against a local inference server — no cloud provider,
no API key, no network egress beyond your own machine (or LAN). This is the
zero-cost tier: local models run at `cost_tier: local`, which the router
always treats as affordable (see [Cost tiers](#cost-tiers-and-the-local-tier)
below), and `input_price_per_mtok`/`output_price_per_mtok` are both `0`.

This works with anything that exposes an OpenAI-compatible `/v1/chat/completions`
and `/v1/models` endpoint: [Ollama](https://ollama.com), [LM
Studio](https://lmstudio.ai), [vLLM](https://github.com/vllm-project/vllm),
and `llama.cpp`'s `server` binary all qualify.

## From zero: installing a local model server

If you have never run a model on your own machine, start here. The whole path is
three commands and one download, and nothing here is bench-specific — you are
installing a small server that holds the model and answers requests on
`localhost`.

The recommended server is **[Ollama](https://ollama.com)**: it is a single
install, it starts itself, it speaks the OpenAI-compatible API bench needs, and
it manages model downloads for you. (LM Studio, vLLM, and `llama.cpp` also work
— see [Other servers](#other-servers) below.)

### 1. Install Ollama

**macOS** — either Homebrew:

```bash
brew install ollama
ollama serve            # leave this running; it's the server
```

...or download the `.dmg` from [ollama.com/download](https://ollama.com/download)
and open the app, which starts the server in the background (no `ollama serve`
needed) and restarts it at login. With the Homebrew install,
`brew services start ollama` gets you the same always-running behaviour.

**Linux** — the official install script (it sets up a systemd service):

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

It starts on install; `systemctl status ollama` confirms it, and
`sudo systemctl enable --now ollama` starts it if your system didn't.

**Windows** — download and run the installer from
[ollama.com/download](https://ollama.com/download). It runs the server in the
background after install; nothing else to start.

### 2. Pull a model that can call tools

bench works almost entirely through *tool calls* (see [the probe](#the-tool-calling-capability-probe)),
so the model must be a tool-calling build. A good default:

```bash
ollama pull qwen2.5:14b-instruct
```

| Model | Download | RAM you want | Notes |
|---|---|---|---|
| `qwen2.5:14b-instruct` | ~9 GB | 16 GB free (24–32 GB total) | recommended default; reliable structured tool calls |
| `qwen2.5:7b-instruct` | ~4.7 GB | 8 GB free (16 GB total) | for ≤16 GB machines; noticeably weaker on multi-step reasoning |
| `llama3.1:8b` | ~4.7 GB | 8 GB free | alternative to the 7B if Qwen misbehaves on your hardware |

If a tag isn't found, drop the suffix (`ollama pull qwen2.5:14b`) — Ollama's
default `qwen2.5` tags are the instruct builds, and tag naming shifts between
model releases. `ollama list` shows what you actually have.

Sizes are the default 4-bit quantized tags Ollama pulls, rounded. The download
is one-time; models live in `~/.ollama` (`C:\Users\<you>\.ollama` on Windows),
not in your bench install. "RAM you want" is *free* memory while the model is
loaded — a 14B model on a 16 GB laptop will run, slowly, and will fight your
browser for memory. On Apple Silicon, unified memory counts.

Expect first-token latency of a few seconds and a small multiple of that per
paragraph on CPU; much faster with a GPU (Ollama uses Apple Silicon's GPU and
NVIDIA cards automatically, no configuration).

### 3. Verify before touching bench

Check the server directly, so that if something is wrong you know it is not
bench:

```bash
curl http://localhost:11434/v1/models
```

Healthy output is JSON listing what you pulled — one entry per model:

```json
{
  "object": "list",
  "data": [
    {
      "id": "qwen2.5:14b-instruct",
      "object": "model",
      "created": 1730000000,
      "owned_by": "library"
    }
  ]
}
```

Two things to confirm: you get JSON back (not `Connection refused`), and your
model's tag appears in `data`. That `id` is exactly what bench will show as
`local/qwen2.5:14b-instruct`.

If you'd rather not install anything at all, bench's compose stack can bring its
own Ollama — see [Bundled via docker compose](#bundled-via-docker-compose).

### Other servers

Ollama is the only server this guide walks through end to end, because it is the
shortest path for someone who has never done this. The alternatives need no
special support from bench — anything with an OpenAI-compatible `/v1` works, and
the only thing that changes is the port you point `BENCH_LOCAL_BASE_URL` at:

- **[LM Studio](https://lmstudio.ai)** — a desktop app with a model browser;
  enable its local server in the Developer tab (`http://localhost:1234/v1`).
  Friendliest if you would rather click than type.
- **[vLLM](https://github.com/vllm-project/vllm)** — the serious option for a
  GPU box serving several people: `vllm serve <model>` gives you
  `http://localhost:8000/v1`. Needs a CUDA-capable GPU and a Python
  environment; it also reports real context windows, which Ollama often doesn't.
- **`llama.cpp`'s `server`** — the lowest-level option, if you are already
  managing GGUF files yourself (`http://localhost:8080/v1`, flag-dependent).

## Setup

Set one environment variable and restart bench:

```bash
BENCH_LOCAL_BASE_URL=http://localhost:11434/v1   # Ollama's default
```

That's the only thing that "enables" the local provider — bench treats a
configured base URL as the credential. No `BENCH_LOCAL_API_KEY` is required;
most local servers ignore the `Authorization` header entirely, but if yours
checks it, set `BENCH_LOCAL_API_KEY` and bench will send it as a bearer token.

Default base URLs per server:

| Server    | Default `BENCH_LOCAL_BASE_URL`     |
|-----------|-------------------------------------|
| Ollama    | `http://localhost:11434/v1`         |
| LM Studio | `http://localhost:1234/v1`          |
| vLLM      | `http://localhost:8000/v1`          |
| llama.cpp `server` | `http://localhost:8080/v1` (flag-dependent) |

`BENCH_LOCAL_DISPLAY_NAME` (default `Local`) is cosmetic — it's the label
prefix shown in the model picker and settings UI.

Once the base URL is set, bench discovers models by calling `GET
{base_url}/models` the first time the model catalog or settings UI asks for
the list. Discovery is cached for 5 minutes (much shorter than the 24h
OpenRouter cache) because local model availability changes far more often —
you'll `ollama pull` or `ollama rm` things mid-session. If the server is down
or unreachable, discovery just returns an empty local model list; it never
raises or blocks anything else in bench from working.

Discovered models are added to the catalog as `local/{model-id}`, e.g.
`local/qwen2.5:14b-instruct` for an Ollama pull. They are always "uncurated"
(`curated: false`) — the static `models.yaml` catalog is never touched by
local discovery.

In the UI, **Settings → Provider keys** shows the `local` row. Before a base URL
is set it offers a short **Set up local models** guide (the three steps above,
with copy-able commands); once one is set it grows a **Test connection** button
that re-reads the server *now* — bypassing the 5-minute discovery cache and
re-probing every model's tool calling — and lists what it found, with a ✓/✗ per
model. That button is the fastest way to answer "did bench see what I just
pulled?". It only ever contacts the server's own configured
`BENCH_LOCAL_BASE_URL`: it accepts no URL from the browser, deliberately, since
an endpoint that fetched a client-supplied URL would be a request-forgery hole
into whatever else the backend can reach. It is admin-only and stores nothing.

### Bundled via docker compose

If you would rather not install anything on your machine, bench's compose stack
can run Ollama for you. Put this in `.env`:

```bash
BENCH_LOCAL_BASE_URL=http://ollama:11434/v1   # the compose service, not localhost
```

...and start the stack with the `local` profile:

```bash
docker compose --profile local up
```

That adds two services: `ollama` (the server, reachable only on the compose
network — no host port is published) and `ollama-init`, a one-shot container
that waits for the server, pulls one model, and exits. It pulls
`qwen2.5:7b-instruct` by default — the smaller model, because inference here is
CPU-only — and `BENCH_LOCAL_PULL_MODEL` overrides that. Re-running the profile
re-downloads nothing: weights live in a named `ollama` volume and survive
`docker compose down`.

Set your expectations before you start it:

- **The first start downloads multi-GB weights.** `docker compose up` will look
  stuck while `ollama-init` pulls; it is downloading, and it logs progress.
- **Inference in the container is CPU-only, and that is slow.** Docker Desktop
  passes no GPU through on macOS, so on Apple Silicon a native
  `brew install ollama` is several times faster than this profile for the same
  model — it gets the GPU, the container never will.
- **This is the zero-manual-steps path, not the fastest one.** Use it to get
  bench running against a local model today; move to a native install (base URL
  `http://host.docker.internal:11434/v1`, which the compose file already makes
  resolvable) once you care about speed.

Keep the base URL in `.env` rather than hard-coding it in the compose file: with
`BENCH_LOCAL_BASE_URL=http://ollama:11434/v1` baked in unconditionally, every
run *without* `--profile local` would show local as configured-but-unreachable.

## Troubleshooting

**"not reachable" / `Connection refused`.** The server isn't running, or isn't
on that port. Check it with `curl http://localhost:11434/v1/models` (step 3
above). On macOS, `ollama serve` must be running in a terminal unless you
installed the app; on Linux, `systemctl status ollama`. If the port differs
(LM Studio is 1234, vLLM 8000), fix `BENCH_LOCAL_BASE_URL` to match — and keep
the `/v1` suffix.

**A model I pulled doesn't appear in the pickers.** Discovery is cached for 5
minutes. Either wait it out, or press **Test connection** in Settings → Provider
keys, which forces a fresh look immediately. If the model is missing there too,
it isn't on the server bench is pointed at: confirm with
`curl http://localhost:11434/v1/models` and check you pulled it into the same
Ollama (a container's Ollama and a native one are different servers with
different volumes).

**A model shows ✗ for tool calling.** It failed bench's forced-tool-call probe,
so it is excluded from routing — this is bench working as designed, not a bug to
route around. Pick a tool-calling build from
[Recommended tool-calling models](#recommended-tool-calling-models) instead;
base (non-`instruct`) models and small distills commonly fail. Very small models
can also fail intermittently on a loaded machine, in which case a larger model
is the fix. `BENCH_LOCAL_PROBE_TOOLS=false` silences the probe rather than
solving anything: it marks every discovered model tool-capable, including ones
that are not.

**Everything works outside docker but not from the compose stack.** Inside a
container, `localhost` is the container itself, not your machine. For a model
server on your host use `http://host.docker.internal:11434/v1` (the compose
file adds the `host.docker.internal` host entry so this also resolves on Linux);
for the bundled profile use `http://ollama:11434/v1`. And check Ollama is
listening beyond loopback if you tightened it — `OLLAMA_HOST=0.0.0.0` on the
server side.

**The probe is slow / Test connection times out.** Each probe is a real
(tiny) generation, so the first one on a cold model includes loading weights
into memory. The connection test caps itself at ~45 seconds total; if a server
with several models exceeds that, test again — the models it has already loaded
answer much faster the second time.

## Recommended tool-calling models

bench's trust model runs almost entirely through tool calls: grading,
structured extraction, and terminal actions are all forced tool calls, not
free-text parsing. A model that doesn't reliably honor `tools`/`tool_choice`
is close to useless in bench, no matter how good its prose is. Pick a build
that's explicitly tuned for tool/function calling:

- **Qwen 2.5 / Qwen 3 Instruct** (7B–32B) — consistently reliable structured
  tool calls in local testing; a good default for extraction and QA-review
  shaped tasks.
- **Llama 3.1 / 3.3 Instruct** (8B–70B) — official tool-calling templates;
  works well through Ollama and vLLM.
- **Hermes / Firefunction fine-tunes** — built specifically for function
  calling, at the cost of some general capability versus the base model.
- **Mistral / Mixtral Instruct** — supported by all four servers, but native
  tool-calling adherence tends to be weaker than the Qwen/Llama tool builds
  above at comparable sizes; verify with the probe (below) before relying on
  one for extraction or grading.

Larger parameter counts generally mean more reliable structured output, not
just better prose — if a small model is passing the probe but producing
malformed arguments in real tasks, the fix is usually a bigger model, not a
different prompt.

## The tool-calling capability probe

Because "OpenAI-compatible" is a spectrum in practice — some servers/models
accept `tools` in the request but silently ignore it, others emit malformed
or hallucinated tool calls — bench never trusts a local model's advertised
capability. Each newly discovered local model is probed once with a trivial
forced tool call (a one-field JSON schema, a few-token response, an 8s
timeout) before it's marked `supports_tools: true`. Models that fail the
probe (timeout, non-tool response, malformed arguments, or any other error)
are marked `supports_tools: false` and **cannot** enter router candidacy or
deterministic fallback — the same rule that already applies to any cloud
model without tool support.

Probe results are cached in memory per model id for the life of the process,
so restarting bench after swapping a model (e.g. pulling a different Ollama
tag under the same name) will re-probe it.

You can disable the probe:

```bash
BENCH_LOCAL_PROBE_TOOLS=false
```

Do this only once you've already independently verified your model's tool
calling — with it off, every discovered local model is assumed to support
tools, no exceptions. Startup and normal usage are never blocked waiting on
the probe either way: discovery (and the probe within it) is fully lazy,
triggered by the same requests that already fetch the OpenRouter catalog, not
by app boot.

## Honest caveats

- **Quality and speed are not cloud-model quality and speed.** A 7B–14B local
  model on consumer hardware will be slower per token and noticeably weaker
  at multi-step reasoning, doctrine-following, and long-context recall than
  Claude Sonnet/Opus, GPT, or Gemini. Local is a good fit for cheap,
  high-volume, low-stakes tasks (routine extraction, drafting scratch text,
  development/testing of a harness before spending cloud budget) — not a
  drop-in replacement for premium-tier verdicts.
- **Tool-calling reliability varies a lot by model and server**, even among
  models that claim OpenAI compatibility. The probe catches outright failures,
  but it is a single trivial call — it does not guarantee a model will hold up
  through a long multi-tool-call agentic loop with a large system prompt. Treat
  a passing probe as "not obviously broken," not as "as reliable as Sonnet."
- **No prompt caching.** `LocalProvider` inherits `OpenAICompatProvider`'s
  no-op cache hook — local servers vary too much in what (if anything) they do
  with repeated prefixes for bench to assume a caching contract exists.
  Usage/cost accounting will show cache reads/writes as zero.
- **Context windows are best-effort.** Not every server reports a usable
  context length from `/models` (Ollama and LM Studio, in particular, often
  don't); when nothing usable is found, bench records `context_window: 0`
  rather than guessing.
- **No network isolation of the model server itself.** Local models run
  wherever you started Ollama/LM Studio/vLLM — same trust boundary as any
  other process on that machine.

## Cost tiers and the local tier

The router's cost-tier gate (`max_cost_tier` in a harness's model policy) only
ever *restricts* how expensive a chosen model can be — `economy` <
`standard` < `premium`. Local models are zero-cost, so they're placed in a
`local` tier that always ranks below `economy`: whatever `max_cost_tier` a
harness is configured with, a discovered-and-probed local model is never
excluded by it.

Because the tier is genuinely at the bottom of that order, it also works as a
ceiling. Set `max_cost_tier: local` and *every* cloud candidate is filtered out,
leaving only local models:

```json
{"mode": "auto", "max_cost_tier": "local"}
```

That is the zero-cloud policy for a single harness — useful when one harness
handles confidential material on a deployment that otherwise has cloud keys
configured. It says "local only" by policy rather than by enumeration, so it
keeps working as you pull and remove models; the `allowed` list is still the
right tool when you want one *specific* model. Note that a harness capped at
`local` cannot run at all if no tool-capable local model is available — routing
fails loudly (`RoutingUnavailable`) instead of quietly falling back to the
cloud, which is the whole point.

The ceiling applies to every routing path, including the two that are easy to
overlook:

- **The deterministic fallback.** When the LLM router step is skipped or fails,
  the fallback table is walked under the same ceiling. It cannot climb out of
  the cap to find something capable; if nothing qualifies it returns nothing and
  the run fails loudly.
- **The router model itself.** Choosing a model is a model call too. If
  `BENCH_ROUTER_MODEL` is above the harness ceiling — the default,
  `anthropic/claude-haiku-4-5`, is above `local` — bench does not consult it for
  that harness and lets the deterministic fallback decide instead. A harness
  capped at `local` therefore makes no cloud request at all, not even to decide
  where to route. Configure a local router model
  (`BENCH_ROUTER_MODEL=local/<model>`) if you want LLM routing on a local-only
  harness.

The persisted `RoutingDecision` reflects this: `router_model` is null whenever no
router was contacted, and `reasoning` names the ceiling that was in force.

If bench has no cloud provider keys configured at all (`BENCH_ANTHROPIC_API_KEY`,
`BENCH_MOONSHOT_API_KEY`, and `BENCH_OPENROUTER_API_KEY` all unset) but a local
server is configured and has at least one tool-capable model, bench still
routes normally: the LLM router step is skipped (no router model is
available), and the deterministic fallback table picks the local model
instead of erroring out.
