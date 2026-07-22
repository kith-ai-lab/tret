# Architecture

```
frontend (React/Vite) ── /api ──> backend (FastAPI) ──> Postgres
                                      │
                                      ├─ engine/    the agent loop (provider-neutral)
                                      ├─ providers/ Anthropic | Kimi | OpenRouter + model catalog
                                      ├─ router_llm/ LLM-as-router + deterministic fallback
                                      ├─ packs/     pack.yaml loader, doctrine hashing
                                      └─ services/  documents, export, bootstrap, credentials
```

## The run lifecycle

1. `POST /api/runs` creates a `runs` row (status `queued`) and schedules
   `HarnessEngine.execute(run_id)` as a background task.
2. **Context assembly** (`engine/context.py`): platform preamble (trust rules)
   → pack doctrine files (stable prefix; Anthropic prompt-cache breakpoint) →
   task instructions + inlined output JSON Schema → harness extras. Documents
   are attached as a manifest; the model reads them via tools, so every read
   is in the audit trail.
3. **Routing** (`router_llm/`): unless the harness pins a model or the run
   overrides one, candidates are filtered (catalog ∩ allowed ∩ cost tier ∩
   provider-has-key ∩ supports-tools) and the router model picks via a forced
   tool call whose `model_id` schema is an enum of the candidates — a
   hallucinated model name is structurally impossible. Timeout/invalid output
   → deterministic fallback table keyed on the task shape. The full decision
   is persisted to `runs.routing` before the first model call.
4. **The loop** (`engine/harness.py`): streaming provider call → SSE events →
   parallel tool execution → repeat. Caps: `max_iterations` and a running
   **cost cap** (priced from the model catalog). Verdict-shaped tasks
   terminate via a `record_verdict` tool call validated against the pack
   schema, with up to 3 in-loop repair attempts on validation errors; the
   cited-values cross-check runs in the same place.
5. Transcript, tokens, and cost are flushed to the DB after every iteration;
   failed runs keep their partial transcripts for the audit view.

## Events / SSE

`GET /api/runs/{id}/events` replays the in-memory backlog then streams live
events (`routing`, `text_delta`, `tool_call`, `tool_result`,
`finding_recorded`, `usage`, `done`, `error`). The event bus is in-process,
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
reiterate) to report them as such. Chat/freeform task types cannot be
delegated to, so delegation cannot recurse.

## Providers

`providers/base.py` defines canonical `Msg`/`ToolCall`/`ToolSpec` types and a
two-method ABC: `stream()` (the main path) and `complete_json()` (forced-tool
structured completion, used by the router). Anthropic uses the native SDK
(content blocks, prompt caching); Kimi and OpenRouter share an
OpenAI-compatible base (httpx + SSE tool-delta aggregation). The
**ModelCatalog** merges a curated static `models.yaml` (authoritative:
prices, tiers, strengths) with an optional dynamic OpenRouter fetch (marked
uncurated). Cost accounting derives from catalog prices.

## Packs

`packs/loader.py` validates `pack.yaml` (pydantic), checks doctrine files,
JSON Schemas, and tool references, computes `doctrine_sha`, inlines schemas
into the stored manifest (no runtime file reads for validation), and seeds
sample datasets. Packs found under `BENCH_PACKS_DIR` auto-install at boot.

## Known v1 constraints

- Single backend worker (in-process event bus) — fine for a team install.
- `search_documents` is substring search; pgvector RAG is the v2 path.
- PDF export uses WeasyPrint; its native libs (pango/cairo) ship in the
  Docker image. A bare local venv without them returns 501 with instructions.
- Pack `tools.py` support is deliberately not loaded yet — pack tools will
  land with a sandboxing story rather than arbitrary in-process code.
- Single workspace; roles are admin / analyst / approver.
