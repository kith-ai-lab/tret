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
DELEGATION_TOOLS = frozenset({"run_harness_task"})

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
