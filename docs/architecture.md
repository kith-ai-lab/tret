# Architecture

```
frontend (React/Vite) ── /api ──> backend (FastAPI) ──> Postgres
                                      │
                                      ├─ engine/    the agent loop (provider-neutral)
                                      ├─ providers/ Anthropic | Kimi | OpenRouter | Local + catalog
                                      ├─ router_llm/ LLM-as-router, objectives, deterministic fallback,
                                      │             recorded outcomes and the priors built from them
                                      ├─ packs/     pack.yaml loader, doctrine hashing, safety scan,
                                      │             content-hash integrity pinning
                                      ├─ net/       the ONLY outbound network path: egress classes,
                                      │             per-URL policy, web search, page snapshots
                                      └─ services/  documents, export, methods (sandboxed compute),
                                                    bootstrap, credentials
```

## The run lifecycle

1. `POST /api/runs` creates a `runs` row (status `queued`) and schedules
   `HarnessEngine.execute(run_id)` as a background task.
2. **Context assembly** (`engine/context.py`): platform preamble (trust rules)
   → pack doctrine files (stable prefix; Anthropic prompt-cache breakpoint) →
   task instructions + inlined output JSON Schema → harness extras. Documents
   are attached as a manifest; the model reads them via tools, so every read
   is in the audit trail.
   A task type may **scope its doctrine** to specific files or `#`/`##` sections;
   an unresolvable selector fails *open* (whole file) so a scoping mistake cannot
   starve a task of its rules. Assembly also produces a **composition report** —
   estimated tokens and a content hash per block — persisted to
   `runs.context_composition`, which is what makes prompt spend legible per
   component rather than as one number.
3. **Routing** (`router_llm/`): unless the harness pins a model or the run
   overrides one, candidates are filtered (catalog ∩ allowed ∩ cost tier ∩
   provider-has-key ∩ supports-tools) and the router model picks via a forced
   tool call whose `model_id` schema is an enum of the candidates — a
   hallucinated model name is structurally impossible. Timeout/invalid output
   → deterministic fallback table keyed on the task shape. The full decision
   is persisted to `runs.routing` before the first model call.
   The harness's **objective** (`model_policy["objective"]`: `quality` |
   `balanced` | `token_conservation` | `eco`, default `balanced` = historical
   behavior) steers all four surfaces: candidate ordering, the rendered prompt's
   `OBJECTIVE` rules, the fallback pick, and the persisted decision. `eco` and
   `token_conservation` rank by estimated energy / cheapest output rather than
   curated-first. See docs/eco-accounting.md.
   The tier order is `local < economy < standard < premium`, so a `max_cost_tier`
   never excludes a zero-cost local model — and setting it *to* `local` excludes
   every cloud candidate, which is how a harness expresses "local only".
   Routing also reads its own **track record**. Every finished run is scored into
   `run_outcomes` (`router_llm/outcomes.py`) from its status, its transcript's
   validation failures and nudges, and — weighted highest — the human
   approve/reject record on the findings it produced. `router_llm/priors.py`
   aggregates those into a per-model record for a (task shape, objective) key,
   with time decay, shrinkage toward same-key peers, and an effective-sample
   floor below which a model has no prior at all: an untried model keeps its
   existing position rather than being ranked last for being untried. Evidence
   enters in three places of deliberately different strength — a `TRACK RECORD`
   section in the router prompt (`route-v4`; omitted entirely when there is no
   evidence, so a fresh install renders the v3 bytes), an evidence tier ahead of
   the objective in candidate ordering, and demotion-only in the deterministic
   fallback. **Nothing derived from evidence can widen a policy**: it reorders
   within `allowed` and under `max_cost_tier`, never past them. Each decision
   snapshots what it read as `runs.routing.evidence`, because the aggregate moves
   and a decision has to stay explicable after it has.

5. **Adapting mid-run** (`engine/compaction.py`, `engine/supervisor.py`): the
   model is chosen once, but a run is not stuck with the consequences.
   Before each call the engine estimates what it is about to send and compares it
   against the chosen model's context window (`model_policy.adaptive.
   context_headroom`, default 0.8, minus the output reservation). Over budget, it
   **compacts**: bulk retrieval results (`read_document`, `search_documents`) are
   replaced by markers naming what was there, and optionally summarized by a
   cheap model. Retrieved values (`lookup_dataset`, `run_method`) and recorded
   results are never elidable — a model may only cite what it retrieved, so
   eliding a dataset result would fail every finding that cited it.
   `runs.messages` remains the **complete, unedited transcript**; compaction
   produces a separate wire view, and `runs.compactions` records the gap.
   Between iterations a deterministic supervisor may **change model**, either
   because the run is over its window with nothing left to elide or because it
   has stalled (repeated terminal-tool validation failures, the repeated-call
   breaker firing, or most of the iteration budget spent with nothing recorded).
   It makes no model call of its own — the priors above supply the judgment. Every
   switch is bounded: within the same policy, never away from a pinned or
   overridden model, never when the forced transcript re-send would not fit the
   remaining cost cap, and at most `max_switches` (default 1) times. Refusals are
   published as `switch_refused`. `runs.model_timeline` records each model's own
   spend and its own energy accounting; `runs.energy_accounting` becomes a
   roll-up whose per-model factors are null wherever the segments disagreed, and
   `model_used` means *the model that produced the final answer*.
   A switch is also the strongest evidence bench can collect — a within-task
   comparison rather than an average across different tasks — so each segment
   becomes its own `run_outcomes` row. A handoff for stalling counts against the
   model; a handoff for running out of context window does not, because a window
   is a size and not a failing.
6. **The loop** (`engine/harness.py`): streaming provider call → SSE events →
   **sequential** tool execution → repeat. A turn's tool calls run one at a
   time, committing after each, and that is a correctness requirement rather
   than a simplification: tools write through the run's single `AsyncSession`,
   which rejects concurrent flushes, and a turn's latency is dominated by the
   provider call anyway. The invariant it buys is **a tool never reports
   failure after its write succeeded, and persisted state never contradicts
   the run's status or events** — so a successful call is committed before the
   model is told it worked, and a failed call is rolled back out of the session
   before the model is told it failed. Caps, all of them because a loop re-sends
   the whole conversation every iteration: `max_iterations` (with a hard ceiling
   above whatever a harness config asks for), a running **cost cap** priced from
   the model catalog, an optional per-run **output-token budget** (soft nudge to
   finalize, then a hard stop), **tool-result size caps** with explicit
   `[TRUNCATED: …]` markers, and a repeated-identical-call breaker. Verdict-shaped
   tasks terminate via a `record_verdict` tool call validated against the pack
   schema, with up to 3 in-loop repair attempts on validation errors; the
   cited-values cross-check runs in the same place.
7. Transcript, tokens (input, output, cache read, cache write), cost, and
   **estimated energy/carbon** (`runs.energy_wh`, `runs.energy_accounting`,
   from each model's energy class × weighted tokens × grid intensity, summed per
   segment) are flushed to the DB after every iteration; failed runs keep their
   partial transcripts for the audit view.
8. **Terminal status.** `completed` | `completed_without_output` | `failed` |
   `cancelled`. The middle one is the honest answer for a run that ended of its
   own accord but never recorded the terminal result its task type requires
   (`engine/harness.py::_completion_status`): not a failure — the engine and the
   guardrails did exactly their job — but not something a caller can read a
   verdict out of either. Tasks with no `terminal_tool` (chat, freeform) always
   end `completed`, since their answer *is* their text. Callers that consume run
   output — the runs list, `run_harness_task`, the chat turn writer — branch on
   this rather than assuming `completed` implies a result.
   "The terminal result landed" is decided by the **engine**, from the task's
   declared `terminal_tool`, when a call to that tool succeeds — never by a tool
   asserting it is terminal. A tool and the task config therefore cannot
   disagree, and `packs/loader.py` additionally rejects at install any task whose
   `terminal_tool` is not among its own `tools`, since a terminal tool the model
   is never offered would strand every run of that task type.

## Outbound network

Everything that leaves the process goes through `bench/net/`, tagged with a
destination **class** — `provider` (cloud model calls), `catalog` (the OpenRouter
model list), `local` (a self-hosted model server), `research` (`web_search` and
`fetch_url`). Each class is independently switchable, every switch narrows and
none widens, and `research` ships off. `tests/test_egress_chokepoint.py` fails
the build if anything outside `bench/net/` imports a connection-opening module or
constructs an HTTP client, so the boundary cannot erode one convenient import at
a time.

Two consequences worth knowing at this level. First, `BENCH_EGRESS=off` is a
working deployment, not a broken one: `local` is exempt (a call to a model server
on your own network never leaves it) and pays for the exemption with a check that
the host really does resolve to a private address, so routing degrades to local
models rather than picking a model it cannot reach. Second, the research class is
the only one whose destination is chosen by a *model*, so it alone resolves and
verifies addresses, follows redirects by hand, caps the body mid-stream, and
writes every call — allowed or refused — to `egress_calls`. See
docs/hardening.md §9 and docs/trust-doctrine.md §1.

## Events / SSE

`GET /api/runs/{id}/events` replays the in-memory backlog then streams live
events: `routing`, `context_composition`, `text_delta`, `tool_call`,
`tool_result`, `finding_recorded`, `usage`, `budget_warning`, `tools_withheld`,
`context_pressure`, `compaction`, `model_switch`, `switch_refused`, `done`,
`error`. (This list had drifted twice over before it was checked against the
code — `context_composition` and `budget_warning` had been publishing
undocumented for some time.) The event bus is in-process,
so the backend runs **one worker by design**. The multi-worker upgrade path
is Postgres LISTEN/NOTIFY behind the same `RunEventBus` interface.

## Chat orchestration

Chat (`api/chat.py`) is a thin layer over the same engine: each user turn
executes as a Run (`task_type='chat'`) with the conversation history injected
and a **capability catalog** — a snapshot of installed pack task types,
persisted in the run's `task_input` — appended to the system prompt. The chat
harness carries the `run_harness_task` tool: the assistant delegates
structured work to a specialist harness, which runs with its own doctrine,
routing, validation, and audit trail. Delegated findings stay drafts behind
the approval gate; the chat agent is instructed (and its tool results
reiterate) to report them as such.

Delegation **can** recurse, and is bounded by a hop counter rather than by what
may be delegated to: any pack task type may list `run_harness_task` among its
tools (or a harness may enable it), so A can delegate to B, B back to A, or a
task to itself. `MAX_DELEGATION_DEPTH` in `engine/tools.py` (2) is the ceiling.
The depth travels with the child run in its `task_input` under
`_delegation_depth`, so the chain is bounded however it was reached: a chat turn
may delegate (0 → 1) and a specialist may delegate one further hop (1 → 2), and
`run_harness_task` refuses past that. Refusing to delegate *to* chat/freeform
task types would not have bounded anything on its own — each hop is a whole
extra agent loop spending its own budget, with only the per-run cost cap in the
way.

## Providers

`providers/base.py` defines canonical `Msg`/`ToolCall`/`ToolSpec` types and a
two-method ABC: `stream()` (the main path) and `complete_json()` (forced-tool
structured completion, used by the router). Anthropic uses the native SDK
(content blocks, prompt caching, with cache reads and writes reported and priced
separately); Kimi, OpenRouter, and any **local** OpenAI-compatible server
(Ollama, LM Studio, vLLM, `llama.cpp`) share an OpenAI-compatible base (httpx +
SSE tool-delta aggregation).

The **ModelCatalog** merges three sources: a curated static `models.yaml`
(authoritative — prices, tiers, strengths, energy classes), an optional dynamic
OpenRouter fetch (uncurated, 24h cache), and local discovery from
`{BENCH_LOCAL_BASE_URL}/models` (uncurated, 5-minute cache, since local
availability changes mid-session). Local models are additionally **probed once
for real tool-calling support** and excluded from candidacy if they fail: bench's
trust model runs through forced tool calls, so a model that ignores `tools` is
not usable here regardless of its prose. Cost accounting derives from catalog
prices (cache reads/writes at their own rates); estimated energy accounting
derives from catalog energy classes (docs/eco-accounting.md).

## Packs

`packs/loader.py` validates `pack.yaml` (pydantic), checks doctrine files,
JSON Schemas, tool references, and doctrine section selectors, computes
`doctrine_sha`, inlines schemas into the stored manifest (no runtime file reads
for validation), and seeds sample datasets. Packs found under
`BENCH_PACKS_DIR` auto-install at boot.

Two integrity layers sit alongside it:

- `packs/safety.py` AST-scans every method entrypoint and fails the pack on
  network access, process spawning, FFI, dynamic import/code, or namespace
  escapes. A deterrent against accidents, explicitly **not** a sandbox.
- `packs/integrity.py` hashes every entry in the pack at install into
  `packs.content_hash` (exposed on `GET /api/packs`) and re-verifies it before
  each method execution, so pack code edited under a running deployment fails
  loudly instead of silently changing results. Regular files contribute their
  bytes; **symlinks contribute their target string** and are never followed, so
  re-pointing one changes the hash — what a directory hash cannot cover is the
  content a link resolves to *outside* the pack, which is pinned by reference
  only (see docs/pack-authoring.md). Re-pin by reinstalling.

`services/methods.py` is the deterministic compute lane: each method runs as a
short-lived isolated subprocess (`python -I`, empty environment, rlimits, wall
clock, output caps, and an empty network namespace on Linux where `unshare` is
available), with its inputs materialized by the runner so the method never
touches the database. Each execution is recorded in `method_runs` with params,
code sha, input summary, and output hash — which is what lets the agent cite a
computed number the way it cites a dataset row. See docs/hardening.md for what
is *not* isolated (the filesystem).

## Schema and migrations

**Alembic owns the schema.** `db/models.py` declares it, `alembic/versions/` is
the only thing that ever applies it to a database, and `db/migrate.py` runs those
migrations from the app's startup path (`ensure_schema`, before any request is
served) using Alembic's Python API on a connection it already holds.

The startup step classifies the database rather than assuming: **empty** →
upgrade from the first revision; **stamped** → upgrade whatever is outstanding;
**legacy** (tables present but no `alembic_version` — a database from v0.1, which
built its schema with `create_all` and left no stamp) → infer the baseline
revision from the columns that actually exist, stamp it, then upgrade. A schema
matching no known revision fails startup with the operator's recovery commands,
because a wrong stamp skips a migration silently. A Postgres advisory lock wraps
the whole step so overlapping instances cannot both migrate. Operator-facing
detail is in docs/upgrading.md.

`create_all` survives in exactly two places, both non-production: the sqlite
fallback for a non-Postgres URL, and `tests/evals/golden_world.py`, which builds
a disposable sqlite world from `Base.metadata` directly. Both are why the
models-vs-migrations drift check exists — CI asserts Alembic autogenerate
produces an empty diff against `Base.metadata` on a real Postgres, so the models
cannot grow a column that no migration adds.

## Known v1 constraints

- Single backend worker (in-process event bus) — fine for a team install.
- `search_documents` is substring search; pgvector RAG is the v2 path.
- PDF export uses WeasyPrint; its native libs (pango/cairo) ship in the
  Docker image. A bare local venv without them returns 501 with instructions.
- Pack-authored **tools** (`tools.py`) are deliberately not loaded — arbitrary
  in-process code needs a sandboxing story first. Pack-authored **methods** are
  the supported path: same expressive power for computation, run out-of-process
  under the controls above.
- The pack safety scan and content-hash pinning are deterrents, not boundaries;
  packs are operator-installed code you are expected to review.
- Guardrail analytics reads validation failures out of recent run transcripts,
  bounded to the last 500 runs — it is a sample, not a ledger. (Energy in the
  same endpoint is a full `SUM` over the window.)
- Single workspace; roles are admin / analyst / approver.
