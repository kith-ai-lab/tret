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
   section in the router prompt (`route-v5`; omitted entirely when there is no
   evidence, so a fresh install renders the v3 bytes), an evidence tier ahead of
   the objective in candidate ordering, and demotion-only in the deterministic
   fallback. **Nothing derived from evidence can widen a policy**: it reorders
   within `allowed` and under `max_cost_tier`, never past them. Each decision
   snapshots what it read as `runs.routing.evidence`, because the aggregate moves
   and a decision has to stay explicable after it has. Every decision also
   carries `effort` (the reasoning-effort level it recorded, sent to the
   provider only when the chosen model's own catalog entry accepts the
   control) and `context_fit` (whether the chosen model's window can hold the
   call, which local models are exempt from that check, and — when adaptive
   compaction is on — whether the floor it was checked against already
   excluded the history that trimming would shrink).
   `POST /api/routing/preview` (`api/routing.py`) answers the same question
   before a run exists: it builds the identical routing inputs (task shape,
   description and output contract from the pack task, `max_output_tokens`/
   `system_prompt_extra`/`tool_names` from the named harness unless a form's
   unsaved edits override them — `tool_names` feeds the same
   pack-task-tools-take-precedence-then-subtract-withheld-web-tools
   derivation `api/harnesses.py::get_harness` uses, rather than trusting a
   client-computed `web_tools_enabled` boolean, though that field is still
   accepted for compatibility — `est_input_tokens` from the same
   `assemble_context`/`composition_report` the engine uses, and
   `min_context_window` from `required_context_window`) and calls `route()`
   for a saved harness's own policy or an inline one a form has not saved
   yet, returning the `RoutingDecision` plus a cost range — the low end prices
   the whole input as a cache *read* plus only 10% of the output budget used
   (the cheapest this call could plausibly be), the high end prices the whole
   input uncached plus the full output budget used — `compare: true` runs it
   again for every other objective, four calls total, and is itself gated to
   the workspace's approver role or higher (`ROLE_RANK` in `api/workspace.py`;
   403 below it) since four router calls is four times the unrecorded spend
   of a plain preview. An inline `model_policy` is validated exactly as a
   saved harness's is (`api/_policy.py`). The permission rule is exact: with
   `harness_id`, a non-admin overriding that harness's policy inline is held
   to its own `max_cost_tier`/`allowed` ceiling, never above it, `mode:
   "pinned"` included — a pin's own `max_cost_tier` field is never read by
   `route()` (it goes straight to the named model), so the check resolves the
   pinned model's actual catalog `cost_tier` instead of trusting the field,
   and a saved `mode: "pinned"` policy's own ceiling is derived the same way
   (its `max_cost_tier` field is equally cosmetic and usually unset, which
   would otherwise leave it with no effective ceiling at all); *without*
   `harness_id` there is no saved ceiling to hold anyone to, so that path
   requires the workspace's admin role or higher outright — harness authoring
   is already admin-gated, and "preview an unsaved new harness" is that same
   workflow. Otherwise a preview would be a way to see what a costlier or
   less confidential model would have done with a harness's data, or, with no
   harness at all, simply any policy an analyst cared to hand it. `pack_id`
   on the harness-id path
   resolves like the inline path (any pack in the caller's workspace, not
   only one already linked to the saved harness — a form previewing a pack it
   just added has no link row yet); omitting `pack_id` still resolves the
   saved harness's own pack for `task_type`, while an explicit `pack_id: null`
   means no pack at all, which only a freeform/chat `task_type` can run
   without one. It is a **dry run** in the sense that matters — nothing is
   persisted, `runs` gets no row, and `engine.execute` is never called — but
   it is not free: each preview makes up to four real short router-model
   calls on the calling workspace's own provider key, and that spend is never
   recorded anywhere in tret's own accounting (it appears on the provider's
   bill and in this endpoint's own log line only). The estimate itself is
   also partial: it covers the system prompt alone — documents, tool specs,
   conversation history and the task input all add to what a real run of the
   same harness will actually send.

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
   produces a separate wire view, and `runs.compactions` records the gap. A
   model switch (below) is preceded by exactly such a pass, forced on the new
   model's first turn even when that turn is not itself over budget — the
   whole transcript is about to be re-sent uncompacted at full price either
   way, so compacting it first is the cheapest possible moment to do so — and
   its record's `trigger` reads `model_switch` rather than the ordinary
   path's `budget`.
   Between iterations a deterministic supervisor may **change model**, either
   because the run is over its window with nothing left to elide or because it
   has stalled (repeated terminal-tool validation failures, the repeated-call
   breaker firing, or most of the iteration budget spent with nothing recorded).
   It makes no model call of its own — the priors above supply the judgment. Every
   switch is bounded: within the same policy, never away from a pinned or
   overridden model, never when the forced transcript re-send would not fit the
   remaining cost cap, and at most `max_switches` (default 1) times. `max_switches:
   0` disables an actual model change but not the effort rung below — raising
   effort spends no switch — and a refusal at that limit says so plainly
   ("this harness does not allow model switches") rather than reporting
   "already changed model 0 time(s)", which reads as if a switch had already
   happened. The engine also skips asking at all — no candidate list, no
   priors lookup, no `switch_refused` event — once a 0 limit leaves nothing
   the rung could still do: it already fired this run, the harness is not
   even under `on_quality` (`on_stall` has no rung to reach), or the current
   model would refuse the effort control anyway. Refusals that are asked for
   are published as `switch_refused`. `runs.model_timeline` records each
   model's own spend and its own energy accounting; `runs.energy_accounting`
   becomes a roll-up whose per-model factors are null wherever the segments
   disagreed, and `model_used` means *the model that produced the final
   answer*. Each segment also keeps a small cache ledger, but only once
   caching has shown itself *live* on that segment — a turn that wrote to the
   cache, or an earlier turn that read a nonzero figure back. From there, a
   turn whose cache read comes back empty for a reason the engine itself
   caused — the segment's own first turn, the forced compaction pass above,
   or a top-level effort raise on Anthropic (which still voids its prompt
   cache) — counts as an *expected* rebuild, while an empty read with none of
   those explanations counts as an *unexpected* miss. Nothing is classified
   before caching has shown itself live: a segment whose every prompt so far
   has been below the provider's cacheable minimum reads 0/0 and stays
   unclassified, the same as a provider that reports no cache figure at all —
   a local deployment, or Kimi's own native API. An OpenRouter-hosted Kimi
   endpoint is *not* excluded by name; it is excluded (when it is) by the same
   live-activity test as any other OpenRouter upstream that never reports
   `cached_tokens`, and would be classified the moment it did.
   `model_policy.adaptive.escalation` (default **`on_quality`**) adds an earlier,
   cheaper trigger ahead of that stall: 2 consecutive terminal-tool validation
   failures, or 1 repeated-call breaker trip — both short of the stall
   thresholds above, because those are the earliest points a deterministic
   signal can call a run non-convergent without also flagging ordinary,
   unhurried work. Its first move is not a switch — it **raises reasoning
   effort on the same model** (low→medium→high, treating no recorded effort
   yet as "low" so the rung still gets its shot) when the model accepts the
   control and has not already been raised this run, because that needs no
   transcript re-send (on Anthropic a top-level effort change still voids the
   prompt cache, but re-pricing one turn is cheaper than a switch's full
   re-send) and so skips the cost-cap check a switch needs. It falls through to
   an ordinary switch only once effort is already at `high`, the model does not
   accept the control, or it was already raised once this run. An effort raise
   updates the *current* segment's `effort` in place — `run.routing[
   "effort_changes"]` still gets the audit record and the segment its own
   `effort_history` — rather than starting a new `ModelSegment`: a same-model
   segment boundary used to leave the segment behind it looking `handed_off`
   to `run_outcomes` (poisoning that model's own prior with a stall it never
   had) and made the run-detail timeline falsely claim the run changed model
   (the UI renders `ModelTimeline` only once a run has more than one segment).
   The rung also carries a grace window: the two counters that triggered a
   raise do not simply retrigger it on the very next iteration, because they
   are running totals that would still be sitting at or past threshold for no
   reason other than never having gone back down. Only what has accrued
   *since* the raise, over at least one full completed iteration, can
   retrigger it, so the raised effort level always gets a real turn before
   anything acts on it again — and the baseline each counter is measured
   against is self-healing rather than a fixed snapshot: once the counter it
   is compared to no longer sits at or above that snapshot (an intervening
   success reset it, or a later model switch reset both explicitly), the
   snapshot is treated as zero, so a plain subtraction can never floor a
   genuinely new run of failures or trips at zero just because a stale,
   higher baseline is still on file. A model switch carries `effort_raised`
   forward (the rung fires once per *run*, not once per model) but clears its
   own snapshot and both counters' baselines, so the trigger is not
   desensitised on the model the switch was meant to give a clean shot at. A
   switch reached this way additionally refuses any target whose recorded
   `delivered_rate` for this shape is *below* the current model's, even where
   its `quality_ci_low` looks better — a model recorded as finishing this shape
   of work less often is not a rescue (Signed Rescue Routing). `escalation:
   on_stall` keeps the plain stall-only behavior with neither the earlier
   trigger nor the effort rung; `off` disables both.
   A switch is also the strongest evidence tret can collect — a within-task
   comparison rather than an average across different tasks — so each segment
   becomes its own `run_outcomes` row. A handoff for stalling counts against the
   model; a handoff for running out of context window does not, because a window
   is a size and not a failing. An effort raise is never such a handoff — it
   stays one segment, and one `run_outcomes` row, for exactly this reason.
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

Beyond a single run's own cost cap (item 6 above), an operator may set a
**per-workspace period spend budget** — daily (the calendar day), weekly (the
ISO week, Monday 00:00 UTC through the following Monday), or monthly (the
calendar month), stored as `Workspace.settings["budget"]` — that looks across
every run in the workspace rather than one at a time. A run is attributed to
the window it *started* in, not the one it finishes in, so a run that crosses
a window boundary counts in full against its starting window. `tret/services/
budgets.py` sums `coalesce(reported_cost_usd, cost_usd)` for runs that
finished in the current window plus `cost_usd` accrued so far by any still
running, and registers its own `PreRunGate` (`engine/extensions.py`) that
refuses a new run (`budget_exhausted`) once that spend plus the run's own
`max_cost_usd` reservation would exceed the cap — a **soft**, informational
reservation, not a hold; a hosted deployment's own credit-hold gate remains
the hard limit, and core's gate is registered first so both run in the same
fail-open order on every deployment. Router/summarizer overhead
(`runs.overhead`) is metered and accounted separately from a run's own
`cost_usd`/`reported_cost_usd`, so period spend is a **lower bound** on what
the workspace actually spent, not the whole of it. A finished run that pushes
the workspace's spend past an alert threshold (50/75/100% by default) logs a
WARNING and publishes a `budget_alert` event on that run's own SSE stream;
which thresholds have already fired for the current window is tracked in
`Workspace.settings["budget_state"]` so each fires once per period.

## Outbound network

Everything that leaves the process goes through `tret/net/`, tagged with a
destination **class** — `provider` (cloud model calls), `catalog` (the OpenRouter
model list), `local` (a self-hosted model server), `research` (`web_search` and
`fetch_url`). Each class is independently switchable, every switch narrows and
none widens, and `research` ships off. `tests/test_egress_chokepoint.py` fails
the build if anything outside `tret/net/` imports a connection-opening module or
constructs an HTTP client, so the boundary cannot erode one convenient import at
a time.

Two consequences worth knowing at this level. First, `TRET_EGRESS=off` is a
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

## SDK and CLI

`tret.sdk.Router` and the `tret run` CLI (`tret/local_run.py`) are a second,
pip-installable entry point into the routing and accounting machinery above —
not a second engine. Both build a `ModelRouter` over the same `get_catalog()` /
`ProviderRegistry` / `NoPriors()` wiring `router_llm/` exposes, and both build
their `Receipt` off the same `services/emissions.energy_accounting()` this
run lifecycle's carbon numbers come from (`tret.sdk._build_receipt`, shared by
both).

What they don't share is `engine/harness.py`. `tret run`'s loop
(`local_run.py::_run_agentic_loop`) is a small, separate single-model loop —
no packs, no doctrine, no approval queue, no compaction, no mid-run model
switching — that mirrors just enough of the harness's message-protocol shape
(one assistant turn, then one tool-result message per call) to stay legible
against it, reimplemented rather than imported because this module sits on
the core (server-free) install path and cannot pull in `engine/*`'s
SQLAlchemy dependency. See [docs/embedding.md](embedding.md).

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
`{TRET_LOCAL_BASE_URL}/models` (uncurated, 5-minute cache, since local
availability changes mid-session). Local models are additionally **probed once
for real tool-calling support** and excluded from candidacy if they fail: tret's
trust model runs through forced tool calls, so a model that ignores `tools` is
not usable here regardless of its prose. Cost accounting derives from catalog
prices (cache reads/writes at their own rates); estimated energy accounting
derives from catalog energy classes (docs/eco-accounting.md).

## Packs

`packs/loader.py` validates `pack.yaml` (pydantic), checks doctrine files,
JSON Schemas, tool references, and doctrine section selectors, computes
`doctrine_sha`, inlines schemas into the stored manifest (no runtime file reads
for validation), and seeds sample datasets. Packs found under
`TRET_PACKS_DIR` auto-install at boot.

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
