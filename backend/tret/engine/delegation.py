"""Delegation constants, in a module with no imports beyond the stdlib.

`engine/tools.py` owns delegation, but it imports the ORM models; `engine/
compaction.py` needs `DELEGATION_TOOLS` and is on the SDK's import path, which
must stay free of server-only packages (tests/test_sdk_import_hygiene.py). So
the constants live here and `tools.py` re-exports them — `from tret.engine.tools
import DELEGATION_DEPTH_KEY, MAX_DELEGATION_DEPTH` keeps working for the engine,
the tests and tret-cloud.
"""
from decimal import Decimal

# ── delegation depth ──────────────────────────────────────────────────────────
# `run_harness_task` starts a whole new run, so delegation is the one tool whose
# cost is another entire agent loop. Refusing chat/freeform task types does NOT
# make it non-recursive: any pack task type may list `run_harness_task` in its
# tools (or a harness may enable it), and then A can delegate to B, B to A, or a
# task to itself — an unbounded chain of runs, each burning its own budget, with
# only the cost cap of the *individual* runs standing in the way. The depth is
# carried in the child run's task_input under `_delegation_depth` and enforced
# here: a chat turn may delegate (depth 0 -> 1) and a specialist may delegate one
# further hop (1 -> 2), and that is the end of it.
MAX_DELEGATION_DEPTH = 2
DELEGATION_DEPTH_KEY = "_delegation_depth"

# Tools that start a child run. Call sites that special-case delegation (the
# compaction elidable set, the chat activity summary) key off this set, so a
# new delegation tool (a parallel batch tool, an ad-hoc subagent tool) inherits
# that handling by being added here rather than by repeating the literal name.
# `delegate_parallel` (engine/tools.py) is the parallel-batch tool this
# comment used to describe as a future addition — it fans several children out
# from one call instead of `run_harness_task`'s one-at-a-time delegation.
# `spawn_subagent` is the ad-hoc-brief tool (also engine/tools.py) — it starts
# a child run too, just one whose task_type/task_input is written by the
# parent's own model rather than resolved against a pack's declared task
# types.
DELEGATION_TOOLS = frozenset({"run_harness_task", "delegate_parallel", "spawn_subagent"})

# Engine-plumbing key (hidden from the model — see `build_user_message`,
# engine/context.py) carrying the ceiling the engine carved for a delegated
# child out of its parent's remaining budget. Read back by the engine at run
# start (harness.py) and combined with the child harness's own cap via `min`,
# so a caller who stamps this by hand through the runs API can only ever
# shrink their own run's cap, never grow it.
COST_CAP_KEY = "_cost_cap_usd"
# Below this, a carved-out child budget cannot buy meaningfully more work than
# the parent could do itself, so the delegation is refused up front rather
# than started with a cap it has no real chance of finishing inside.
MIN_CHILD_BUDGET_USD = Decimal("0.05")

# Engine-plumbing key carrying the short, model-written name a caller gave a
# child via the delegation tool's `label` argument (`_prepare_child`'s
# `label` parameter). Persisted on the child's own task_input so it survives
# past the turn that started it — the `delegation_started`/`delegation_finished`
# events carry the label live, but a completed turn and the run-detail
# children table only have the child run's row to read it back from. Stamped
# by the engine from the tool argument alone: a model-supplied `_label`
# already sitting in a TASK child's free-form task_input must never win over
# it (see `_prepare_child`), so it is dropped whenever no label was given.
LABEL_KEY = "_label"

# ── ad-hoc subagents ──────────────────────────────────────────────────────────
# A subagent is a child run whose brief is written by the parent MODEL (via a
# future `spawn_subagent` tool), not declared by a pack — the third member of
# `engine/harness.GENERIC_TASK_TYPES`. Its constants live here, next to the
# other delegation ones, for the same reason: `tools.py` owns the run but
# `compaction.py` needs to reason about what a subagent tool call looked like
# without importing the ORM.
SUBAGENT_TASK_TYPE = "subagent"
# `Harness.task_profile` of the one harness every workspace seeds for this
# (`services.workspace.seed_subagent_harness`) — never a `run_harness_task`
# target (see `_prepare_child`'s harness query in tools.py).
SUBAGENT_TASK_PROFILE = "subagent"
# task_input keys the engine reads when starting a subagent run.
ALLOWED_TOOLS_KEY = "_allowed_tools"  # list[str]: the parent's own grant, narrowing further
PROJECT_DOCS_KEY = "_project_docs"  # bool: the parent itself had project-wide document scope

# An ALLOWlist, not a denylist, so a write tool added to the engine later is
# denied to a subagent by default rather than needing to be remembered here.
# A subagent's brief is model-written and may be poisoned by something the
# parent read while doing its own work, so it must never hold a tool its
# parent lacks, never hold any write/record/delegation tool, never see more
# documents than its parent, and only ever REPORT text back — everything that
# records, proposes, files or delegates (`record_verdict`, `record_finding`,
# `propose_connected_write`, `propose_pack_lesson`, `file_data_request`,
# `draft_section`, and every `DELEGATION_TOOLS` member) is deliberately absent
# from this set.
SUBAGENT_ALLOWED_TOOLS = frozenset(
    {
        "read_document",
        "search_documents",
        "lookup_dataset",
        "list_prior_findings",
        "list_pack_lessons",
        "run_method",
        "web_search",
        "fetch_url",
        "list_connected_sources",
        "search_connected_files",
        "read_connected_file",
    }
)
