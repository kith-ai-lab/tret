# Evals — a tret for tret

The trust guarantees in the README are only worth something if they cannot
quietly stop being true. Prompts get reworded, doctrine gets edited, a router
change picks a different model, a provider adapter changes how tool calls are
emitted — none of that shows up as a stack trace. It shows up as output that
looks fine and is no longer grounded.

The eval suite is the regression gate for that class of change. It lives in
`backend/tests/evals/` and runs offline, deterministically, as part of the
normal test suite:

```bash
cd backend && .venv/bin/python -m pytest tests/evals -q
```

## What a golden run is

A **golden run** drives the real engine end to end against a **scripted model**.
Everything except the model is production code:

| Component | Golden run uses |
| --- | --- |
| Agent loop (`engine/harness.py`) | real |
| Pack loading, doctrine, JSON Schemas (`packs/climate-risk/`) | real, from disk |
| Tools (`engine/tools.py`) | real, against seeded sample data |
| Validation (`engine/validation.py`) | real |
| Deterministic methods (`services/methods.py`) | real, subprocess and all |
| Storage | real models, on a throwaway sqlite file |
| Model | `ReplayProvider` — a scripted `Provider` |

Because the model is the only fake, a golden run answers a precise question:
*given a model that behaves exactly like this, does the harness still produce a
grounded, schema-valid, fully audited result?*

### The pieces

- **`replay_provider.py`** — `ReplayProvider` implements the `Provider` ABC.
  It plays a list of `ScriptedTurn`s: each `stream()` call emits that turn's
  text deltas, its tool calls, and a `TurnComplete`, so it walks the tool loop
  like a live model — emit tool calls, receive results on the next call, then
  emit the terminal structured output. It also implements `complete_json()`, so
  router calls stay offline too.

  A turn's `arguments` may be a **callable** that receives the conversation so
  far. That is how a script cites values it actually retrieved (`rows_of`,
  `find_row`, `cite`) instead of values hardcoded in the test — the same
  discipline the real model is held to. Hardcode a value only when the point of
  the scenario is that it was never retrieved.

- **`golden_world.py`** — `GoldenWorld` builds a disposable tret: sqlite
  schema, a workspace/project/user, and the real pack installed from disk with
  its sample data seeded. `world.run(...)` creates a run and executes it
  through `HarnessEngine`, then reads back everything a scenario asserts on:
  the run row, findings, data requests, method runs, and the full `RunEvent`
  stream.

- **`test_golden_runs.py`** — the scenarios for the flagship verdict task.

- **`test_golden_write_integrity.py`** — the scenarios for the write-integrity
  invariant: *a tool never reports failure after its write succeeded, and the
  run's persisted state never contradicts its status or its events.* These cover
  the shapes `test_golden_runs.py` structurally cannot: **multi-write turns**
  (several writing tool calls in one turn) and the **non-`record_verdict`
  terminal tools** the pack actually declares.

- **`test_live_eval_example.py`** — the live-eval skeleton (see below).

- **`test_subagent_tools_offered.py`** — what `spawn_subagent` actually offers
  an ad-hoc subagent, through the real engine rather than a unit-level stub:
  the read-only allowlist intersected with what the parent run itself was
  granted, narrowed further by an explicit request, and never widened by a
  later step in the loop. The delegation tools themselves have unit-level
  coverage instead of a golden scenario each — `test_delegate_parallel.py`,
  `test_spawn_subagent.py`, and `test_delegation_phases.py` (`backend/tests/`)
  — since what they exercise (fan-out width, budget carving, the
  prepare/run/record phase split) is about the tool call's own mechanics, not
  a task's doctrine or output shape.

### Guardrails on the evals themselves

`HarnessEngine.execute()` catches every exception and records it as
`run.error`, so a broken script could look like a passing eval. Two things
prevent that:

- The `ReplayProvider` records **violations** (script exhausted, or a scripted
  tool the engine never offered) and `world.run()` re-raises them as
  `AssertionError` after the run.
- `test_script_exhaustion_fails_loudly` and
  `test_calling_a_tool_the_pack_does_not_enable_fails_loudly` assert that both
  guardrails fire. If a pack task loses a tool, the eval breaks loudly instead
  of passing vacuously.

## The scenarios and what each one locks in

### `test_golden_runs.py` — the flagship verdict task

All of these use *Signal divergence assessment* for site `S-003` (Alder Point) ×
`flood`, except `test_method_output_is_citable_but_the_same_number_alone_is_not`,
which drives the `portfolio_divergence_rate` method on a `freeform` harness
instead — see [Known gaps](#known-gaps) for why the method-citation scenario
does not run on a shipped verdict task.

| Test | Guarantee |
| --- | --- |
| `test_happy_path_records_a_grounded_schema_valid_verdict` | The AI never invents numbers. Every `cited_values` entry matches a `(dataset, row_ref, value)` the run actually retrieved, the payload is schema-valid, the finding is a `draft`, and provenance carries the model and doctrine hash. |
| `test_happy_path_run_events_form_an_audit_trail` | Every tool call, tool result, usage tick, recorded finding, and the terminal `done` event is published — the audit trail the UI and the export read. |
| `test_happy_path_context_carries_doctrine_schema_and_pack_tools` | The prompt contract: platform rules, doctrine loaded with per-file content hashes, the task instructions, the output schema, and exactly the tool set the pack's task type declares. `divergence_assessment` declares no [task-scoped doctrine](pack-authoring.md#task-scoped-doctrine) of its own, so it gets every doctrine file the pack has (three, today) rather than a subset; the test checks two of them by name (the principles and the divergence procedure) as a representative sample, not an exhaustive count. Reword the preamble or drop a doctrine file this task relies on and this fails. |
| `test_hallucinated_number_is_a_validation_error_not_a_finding` | A cited value that was never retrieved is rejected with the exact failure text, no finding is written, the error is fed back in-loop for repair, and the engine nudges once for the missing terminal verdict. |
| `test_hallucination_repair_budget_is_finite` | Three bad attempts exhaust the repair budget and say so, rather than looping. |
| `test_a_run_that_records_its_verdict_is_plainly_completed` | The pair to the two above: a run that lands no valid verdict ends `completed_without_output` (not `completed`, not `failed`), keeps `error` null, and still emits `done` carrying that status — while a run that does record its verdict stays plainly `completed`. |
| `test_missing_data_yields_insufficient_data_and_a_data_request` | `insufficient_data` is a respectable outcome: an empty lookup reads as a legible instruction, a `DataRequest` is filed, the verdict validates without a reason code, and the run still completes. |
| `test_method_output_is_citable_but_the_same_number_alone_is_not` | The deterministic lane: the *same* number is rejected when cited without a method run behind it and accepted when quoted from a real `run_method` result, whose manifest (params, code hash, input summary, output hash) is persisted. |
| `test_golden_run_is_reproducible` | The same script produces byte-identical citations, twice. |

### `test_golden_write_integrity.py` — writes, events, and status agree

Every scenario in the table above records exactly one write per turn, through
`record_verdict`, on one task type. Two mainline bugs lived in the gap: the loop
executed a turn's tool calls concurrently over the run's single `AsyncSession`
(so the second writer's row was committed while the model was told it failed),
and the terminal flag was set by each tool about itself (so `record_finding` —
the declared `terminal_tool` for `evidence_extraction` *and* `qa_review`, half
the shipped pack — never set it, while `draft_section` set it unconditionally).

| Test | Guarantee |
| --- | --- |
| `test_evidence_extraction_completes_on_its_declared_terminal_tool` | A task whose `terminal_tool` is `record_finding` reaches `completed` with its finding present, un-nudged. The engine never claims a result is missing when it is on disk. |
| `test_every_write_in_a_multi_write_turn_persists_and_reports_success` | Three `record_finding` calls in one turn — the pack's own prescribed shape. All three persist exactly once, all three are reported to the model as successes, and there is one `finding_recorded` event per finding, in order. |
| `test_a_data_request_and_the_verdict_in_one_turn_agree_with_the_run_status` | Two writers in one turn, the second of them the terminal tool: both writes land, both report success, and status / events / rows give the same answer. |
| `test_qa_review_completes_on_its_declared_terminal_tool` | The pack's other `record_finding` task type, end to end over a prior finding — and reviewing does not overwrite what it reviewed. |
| `test_draft_section_does_not_satisfy_a_validated_terminal_tool` | The other half of the flag: on a task whose declared terminal tool validates a schema, an unvalidated `draft_section` write does *not* mark the run `completed`. Runs on a minimal throwaway pack, since no shipped task enables that combination. |

`assert_no_false_nudge` and `assert_status_agrees_with_findings` in that file are
the reusable form of the invariant — apply them to any new scenario that writes.

### `test_engine_loop.py` — the grounding check

`tret/engine/grounding.py`'s in-loop number check on chat/freeform prose — see
the trust-doctrine.md paragraph after cited-values. All ten drive `freeform`
or the flagship verdict task through the real engine with a scripted model;
none touch the grounding module's own extraction/evidence/rounding/percent/
arithmetic logic, which is covered offline in `tests/test_grounding.py`.

| Test | Guarantee |
| --- | --- |
| `test_a_fabricated_figure_gets_one_grounding_nudge_then_completes` | A reply citing numbers no `lookup_dataset` call this run returned (and nothing in the conversation said) gets exactly one nudge naming them, a clean rewrite completes the run, and `run.grounding` records `status: "repaired"` with the figures that triggered the first nudge preserved under `first_unsupported`. |
| `test_a_reply_that_repeats_a_lookups_own_no_match_filter_is_still_flagged` | `lookup_dataset`'s own "No rows ... match {filters}" message echoes the filter values it was called with — including a number the model put there itself. That echo does not launder the number into evidence: the reply still gets nudged for citing it. |
| `test_a_reply_that_only_cites_retrieved_and_user_numbers_is_never_nudged` | A reply that only repeats a retrieved value and a number the user themselves stated is never nudged; `run.grounding` reads `status: "clean"`, `attempts: 0`. |
| `test_numbers_from_conversation_history_are_accepted_as_evidence` | A number from a user-role entry of `task_input["_history"]` — an earlier turn's own words — counts as evidence even when nothing in this run's own tools or system prompt repeats it (a `lookup_dataset` call is scripted purely so the check runs at all rather than being `skipped`). |
| `test_a_repeated_fabricated_figure_is_never_self_evidence` | A rewrite that just repeats the same fabricated number is not let off the hook by citing its own earlier, rejected turn — no assistant turn from this run (or an earlier one, via `_history`) is ever evidence, so the same "58" fails all three times it appears, exhausting the budget for exactly two nudges. |
| `test_three_grounding_failures_in_a_row_exhaust_the_repair_budget` | `GROUNDING_MAX_REPAIRS = 3`: the model gets two rewrites; a third, differently-fabricated reply in a row is kept exactly as the model wrote it (never blanked) rather than asked for a fourth rewrite, and `run.grounding` reads `status: "unresolved"`, `attempts: 3` — the run's own status is unaffected. |
| `test_a_grounding_failure_on_the_last_iteration_is_not_nudged` | A failing reply that lands on the run's own last iteration is never nudged — there is no turn left for the rewrite it would ask for — and ships as-is, flagged `unresolved`; the run still ends `completed`, not `failed` by the iteration ceiling. |
| `test_a_pure_knowledge_reply_is_never_checked_no_retrieval_to_contradict` | A run that never calls a tool and never retrieves anything is not checked at all: `run.grounding` reads `checked: False, status: "skipped"`, distinct from `"clean"`. |
| `test_grounding_is_not_written_over_an_empty_final_reply` | A fabricated reply gets nudged, then two empty replies in a row end the run `completed_without_output` — the empty-reply guard owns that outcome, and `run.grounding` is left `None` rather than backfilled with a stale or spurious verdict. |
| `test_a_verdict_task_never_checks_grounding` | A task with a declared `terminal_tool` (`divergence_assessment`) is already held to the cited-values cross-check on its structured output — the grounding check never runs on it, `run.grounding` stays null, and no grounding nudge ever appears in its transcript. |

## Adding a golden scenario

1. Decide which guarantee is unprotected. A scenario that does not fail when a
   guarantee is broken is not worth its runtime.
2. Write the script as a list of `ScriptedTurn`s. Derive tool arguments from
   prior tool results wherever the scenario is about grounded output:

   ```python
   def _verdict(messages):
       rows = rows_of(messages, "lookup_dataset", call_index=1)
       row = find_row(rows, scenario="mid_emissions")
       return {
           "schema_slug": "divergence_verdict",
           "subject": {"site_id": "S-003", "peril": "flood"},
           "payload": {..., "cited_values": [cite(row, "median_delta")]},
       }
   ```

3. Remember the trailing turn. After the terminal tool succeeds the engine asks
   the model once more; a turn with no tool calls ends the run. If the terminal
   tool never succeeded, the engine nudges once first, so script that turn too.
4. Run it, then assert on `result.run`, `result.finding`, `result.tool_errors`,
   `result.data_requests`, `result.method_runs`, `result.events`, and
   `result.provider.calls` (what the model was actually shown).
5. Use `assert_citations_grounded(finding, world.output_schema(slug))` for any
   scenario that records a payload with `cited_values`, and `assert_auditable`
   for any scenario that should complete.

## Policy

**Prompt, doctrine, routing, provider, and pack changes must keep the golden
runs green.** They run in the normal `pytest` suite, so this is enforced
wherever the suite is.

When a golden run fails, exactly one of two things is true:

- **The change is a regression.** Fix the change.
- **The change is intentional and the expectation is now wrong** — e.g. the
  preamble was deliberately reworded, or a task's tool list changed on purpose.
  Then update the expectation **in the same commit as the change**, and say so
  in the commit message. A golden expectation updated in a separate "fix tests"
  commit is indistinguishable from a regression that was papered over.

Never make a golden run pass by loosening it (dropping the exact failure text,
removing an assertion, widening a set). Loosening an eval is a change to what
tret promises.

## Live evals

Golden runs prove the *harness* holds. They say nothing about whether a real
model is good enough — a scripted model does whatever the script says. Live
evals answer the other question: **can this model, given the real pack, satisfy
the guarantees on its own?**

The skeleton is `test_live_eval_example.py`. It reuses the same world, pack, and
assertions; the only difference is that no provider is injected, so the engine
builds its own registry from configured keys and a real model drives the loop.

Live evals are marked `live` and **skipped by default** (registered in
`backend/pyproject.toml`, gated in `backend/tests/conftest.py`):

```bash
TRET_LIVE_EVALS=1 \
TRET_EVAL_MODEL=anthropic/claude-sonnet-5 \
TRET_ANTHROPIC_API_KEY=sk-... \
  .venv/bin/python -m pytest tests/evals -m live -q
```

`TRET_EVAL_MODEL` is a catalog model id (see
`backend/tret/providers/models.yaml`); it defaults to the golden pin. Keep live
evals out of CI: they cost money and a model's mood is not a build gate. They
belong in a periodic, manually reviewed sweep — the natural next step is to run
the matrix across catalog models and record verdict agreement, repair-attempt
counts, iterations, and cost per model, which is exactly what a live eval
already returns.

## Known gaps

Deliberately not covered yet:

- **No shipped task type combines `run_method` with a `cited_values`-validating
  terminal tool.** `tcfd_section_draft` has `run_method` but its terminal
  `draft_section` payload is unvalidated markdown; `divergence_assessment`
  validates citations but does not enable `run_method`. The method-citation
  scenario therefore runs on a `freeform` harness with both tools enabled. The
  engine supports the combination; the pack does not yet use it.
- **`tcfd_section_draft` has no golden run.** `evidence_extraction` and
  `qa_review` now do (`test_golden_write_integrity.py`; extraction attaches the
  pack's own `acme-esg-questionnaire.md` via `world.create_document`, QA reviews
  the finding a prior happy-path run recorded). Drafting is the remaining
  uncovered task type: its terminal payload is unvalidated markdown, so a
  scenario for it would assert on prose rather than on a schema, and the
  `draft_section` *flag* behaviour it shares is already locked by
  `test_draft_section_does_not_satisfy_a_validated_terminal_tool`.

  This entry read "the pack's other task types have no golden runs" and was the
  proximate cause of two shipped trust bugs: with every scenario driving
  `divergence_assessment` → `record_verdict`, nothing exercised a
  `record_finding` terminal path or a turn with more than one write, and both
  were broken on the mainline. A known gap in eval coverage is a known gap in
  the guarantees, not a backlog item.
- **Chat delegation** (`run_harness_task`) is uncovered: it opens a nested
  session inside the parent's, which needs a second look under the sqlite
  harness before it is scripted.
- **Prompt caching and streaming shapes** (whether a provider emits the same
  events under partial tool-call deltas) are provider-adapter concerns; a
  golden run drives `ReplayProvider`, not the real adapters.
- The sqlite shim (`install_sqlite_type_shims`) rewrites `ARRAY` columns to
  JSON **process-wide** for the test session. That is invisible today because
  no other test exercises Postgres array behaviour; a test that needs real
  `ARRAY` SQL will need a Postgres fixture instead.

## Routing benchmark

The golden runs above lock in *correctness* under a scripted model; they say
nothing about whether the router's own choices are worth what they cost. That
question lives in `backend/benchmark/arm_routing.py`, a third benchmark arm
alongside Arm A/B (`backend/benchmark/README.md`): it drives the real engine
through the API under `auto` routing at each objective and `pinned` routing to
specific models, and `scoring_routing.py` turns the results into a
per-configuration table plus a router-vs-pinned cost/agreement comparison.
Like the golden runs and Arm A/B, it costs real money to run and is not part
of the offline test gate (`backend/tests/test_benchmark_routing_scoring.py`
covers only the scoring math, on hand-built rows, with no engine or network
involved).
