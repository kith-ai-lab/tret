"""runs.conversation_id — attribute a run to the chat turn that produced it

Revision ID: 9ba228f09f91
Revises: 5540e56092f1
Create Date: 2026-09-16 00:00:00.000000

Backfill, two passes:

1. `conversations.messages` is a JSONB array whose entries carry a `run_id`
   per chat turn (`api/chat.py::_assistant_message`) — the direct link for
   every top-level chat-turn Run. This is the only shape `conversations.messages`
   actually carries; an earlier draft of this migration also read
   `activity[].child_run_id` off the same JSONB, on the strength of a doc
   comment on `Conversation.messages` (tret/db/models.py) that promised that
   shape. Nothing ever writes it: `_assistant_message` builds each `activity`
   entry as `{"tool", "summary"}` only, never a `child_run_id` — checked
   directly against that function, not just the doc comment (which was wrong
   and is fixed alongside this migration). That branch matched nothing in any
   real database and has been removed; a test asserting it up used
   hand-written JSON in a shape production never emits, which is how it went
   unnoticed.

2. A delegated run's id IS recoverable, just not from `conversations.messages`.
   `run_harness_task` (engine/tools.py) returns a JSON object containing
   `child_run_id` as its tool result, and tool results are persisted verbatim
   into the *delegating run's own* `runs.messages` (as a `{"role": "tool",
   "content": <that JSON, as text>}` entry — `providers/base.py::Msg.to_json`,
   written by `engine/harness.py`). So once a run has a `conversation_id` —
   whether it is a top-level chat turn from pass 1, or a delegated run
   attributed by an earlier iteration of this same pass — its own `messages`
   name any run IT delegated to, and that child inherits the same
   `conversation_id`. Repeated `_MAX_DELEGATION_PASSES` times so a
   grandchild (a delegated run's own delegation) is reached too, matching
   `MAX_DELEGATION_DEPTH` in engine/tools.py as of this writing — see the
   constant below for why this migration hardcodes that number rather than
   importing it.

Both passes are batched, not one UPDATE across the whole table: this runs at
boot, including against the 256MB production instance, and a single statement
exploding every conversation's or run's `messages` array into rows at once
would size Postgres's work_mem and its lock/WAL footprint against the *whole*
table rather than one slice of it.

Batched but NOT `autocommit_block()`, tempting as per-batch commits are here:
this project applies migrations through `db/migrate.py::ensure_schema`, which
calls `command.upgrade` inside `conn.run_sync` on a connection that already
owns a transaction. `autocommit_block()` ends that transaction to get its own,
and alembic's own bookkeeping then trips an assertion on the way out —
migrations run at boot, so that is an app that will not start. The batching
still bounds each statement; the whole backfill simply lands in one
transaction, which is the right trade at any size this deployment will see.

Ordering inside that transaction, and why: `add_column` first (trivial), then
the FK — cheap here specifically *because* it runs before either backfill
pass, so every row's `conversation_id` is still NULL and the FK's validating
scan has nothing to check against `conversations` (NULL trivially satisfies a
foreign key); creating it after backfilling instead would make it validate
every newly non-null row against `conversations` for real. Then both backfill
passes. The index is created LAST, after both backfills, not alongside the FK
the way an earlier draft had it: building it before the backfill would have
meant every batch's UPDATEs were also maintaining a brand-new index as they
wrote — roughly doubling the backfill's WAL and blocking HOT updates — for no
benefit, since nothing queries this table by conversation_id until the
backfill (and the app) is already running.
"""
from alembic import op
import sqlalchemy as sa

revision = "9ba228f09f91"
down_revision = "5540e56092f1"
branch_labels = None
depends_on = None

# A plain (non-capturing-group-anchored) UUID shape, reused by both passes to
# guard every ::uuid cast below.
_UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# Rows processed per batch (conversations in pass 1, runs in pass 2). Small
# enough that one batch's worth of `messages` arrays (and the rows
# `jsonb_array_elements` explodes them into) stays well inside a 256MB
# instance's default work_mem; large enough that a realistic install
# (thousands of conversations, not millions) backfills in a handful of round
# trips rather than thousands of them.
BATCH_SIZE = 500

# Number of times pass 2 (delegation propagation) repeats. A delegated run is
# one hop from its parent; a grandchild (a delegation's own delegation) is
# two. `engine/tools.py::MAX_DELEGATION_DEPTH` bounds how deep that can go —
# currently 2 — and the live path (that same module, at delegation time)
# attributes every hop as it happens, so this backfill must walk the same
# number of hops or a freshly-migrated database would disagree with a
# freshly-created run about how far attribution reaches.
#
# Hardcoded, not imported from engine/tools.py: a migration is a frozen
# snapshot of what a given release needed to do to the schema, and importing
# app code into it means a future change to MAX_DELEGATION_DEPTH silently
# rewrites what THIS migration does when it runs against an old release's
# database — exactly the kind of drift migrations exist to prevent. If that
# constant ever changes, a new migration should extend attribution for
# databases that already ran this one; this number should not.
_MAX_DELEGATION_PASSES = 2

# One batch's worth of pass 1: every top-level run_id named by this batch's
# conversations, attributed to whichever conversation named it, applied to
# `runs` in a single UPDATE ... FROM (grouped in SQL, not fetched into Python
# and looped over one at a time).
#
# `messages` itself is guarded with `jsonb_typeof` before `jsonb_array_elements`
# — a hand-edited or partially restored `conversations` row whose `messages`
# is not a JSON array (or is JSON null) must not raise and abort the whole
# batch's transaction; treating it as `[]` skips just that row's contribution.
# The extracted `run_id` is cast to uuid only inside a `CASE WHEN ... THEN
# ...END`, never as a bare `::uuid` reachable from a WHERE clause a planner
# could evaluate in either order — Postgres does not guarantee a WHERE
# clause's sub-expressions run in the order they are written, so a cast
# gated only by a sibling condition (`x ~* '...' AND y = x::uuid`) can still
# be attempted against a non-uuid `x` and raise. Gating the cast itself inside
# the CASE means no plan can reach it except when the regex already matched.
_BACKFILL_BATCH_SQL = sa.text(
    f"""
    WITH batch_conversations AS (
        SELECT id, messages FROM conversations WHERE id IN :ids
    ),
    entries AS (
        SELECT bc.id AS conversation_id, entry
        FROM batch_conversations bc,
             jsonb_array_elements(
                 CASE WHEN jsonb_typeof(bc.messages) = 'array' THEN bc.messages ELSE '[]'::jsonb END
             ) AS entry
    ),
    candidate_run_ids AS (
        SELECT conversation_id,
               CASE WHEN entry ->> 'run_id' ~* '{_UUID_RE}'
                    THEN (entry ->> 'run_id')::uuid END AS run_id
        FROM entries
    )
    UPDATE runs
    SET conversation_id = candidate_run_ids.conversation_id
    FROM candidate_run_ids
    WHERE runs.id = candidate_run_ids.run_id
      AND runs.conversation_id IS NULL
      AND candidate_run_ids.run_id IS NOT NULL
    """
).bindparams(sa.bindparam("ids", expanding=True))

_NEXT_CONVERSATION_BATCH_SQL = sa.text(
    "SELECT id FROM conversations WHERE id > :last_id ORDER BY id LIMIT :limit"
)
_FIRST_CONVERSATION_BATCH_SQL = sa.text("SELECT id FROM conversations ORDER BY id LIMIT :limit")


def _backfill_from_conversations() -> None:
    bind = op.get_bind()
    last_id = None
    while True:
        query = _FIRST_CONVERSATION_BATCH_SQL if last_id is None else _NEXT_CONVERSATION_BATCH_SQL
        params = {"limit": BATCH_SIZE} | ({} if last_id is None else {"last_id": last_id})
        ids = [row[0] for row in bind.execute(query, params).fetchall()]
        if not ids:
            break
        bind.execute(_BACKFILL_BATCH_SQL, {"ids": ids})
        last_id = ids[-1]
        if len(ids) < BATCH_SIZE:
            break


# One batch's worth of one pass of delegation propagation: every run in this
# batch that already has a conversation_id is a candidate *parent*; its own
# `messages` is scanned for `role: "tool"` entries (a delegation's result,
# per `run_harness_task`) and the child run id is pulled out of the result
# JSON's `content` text with a regex, not a `::jsonb` cast of that text — most
# tool results are plain prose, not JSON, and `regexp_match` simply returns no
# match against those instead of raising the way a failed jsonb cast would.
# The same `jsonb_typeof` guard on `messages` itself and the same CASE-gated
# uuid cast as pass 1 apply here for the same reasons.
_PROPAGATE_BATCH_SQL = sa.text(
    r"""
    WITH batch_parents AS (
        SELECT id, conversation_id, messages FROM runs
        WHERE id IN :ids AND conversation_id IS NOT NULL
    ),
    tool_entries AS (
        SELECT bp.conversation_id AS conversation_id, entry
        FROM batch_parents bp,
             jsonb_array_elements(
                 CASE WHEN jsonb_typeof(bp.messages) = 'array' THEN bp.messages ELSE '[]'::jsonb END
             ) AS entry
        WHERE entry ->> 'role' = 'tool'
    ),
    extracted AS (
        SELECT conversation_id,
               (regexp_match(entry ->> 'content', '"child_run_id":\s*"([0-9a-fA-F-]+)"'))[1]
                   AS child_run_id
        FROM tool_entries
    ),
    candidate_child_ids AS (
        SELECT conversation_id,
    """
    f"""
               CASE WHEN child_run_id ~* '{_UUID_RE}' THEN child_run_id::uuid END AS child_run_id
    """
    """
        FROM extracted
        WHERE child_run_id IS NOT NULL
    )
    UPDATE runs
    SET conversation_id = candidate_child_ids.conversation_id
    FROM candidate_child_ids
    WHERE runs.id = candidate_child_ids.child_run_id
      AND runs.conversation_id IS NULL
      AND candidate_child_ids.child_run_id IS NOT NULL
    """
).bindparams(sa.bindparam("ids", expanding=True))

_NEXT_RUN_BATCH_SQL = sa.text("SELECT id FROM runs WHERE id > :last_id ORDER BY id LIMIT :limit")
_FIRST_RUN_BATCH_SQL = sa.text("SELECT id FROM runs ORDER BY id LIMIT :limit")


def _propagate_one_pass() -> None:
    bind = op.get_bind()
    last_id = None
    while True:
        query = _FIRST_RUN_BATCH_SQL if last_id is None else _NEXT_RUN_BATCH_SQL
        params = {"limit": BATCH_SIZE} | ({} if last_id is None else {"last_id": last_id})
        ids = [row[0] for row in bind.execute(query, params).fetchall()]
        if not ids:
            break
        bind.execute(_PROPAGATE_BATCH_SQL, {"ids": ids})
        last_id = ids[-1]
        if len(ids) < BATCH_SIZE:
            break


def _backfill_conversation_id() -> None:
    _backfill_from_conversations()
    for _ in range(_MAX_DELEGATION_PASSES):
        _propagate_one_pass()


def upgrade() -> None:
    op.add_column("runs", sa.Column("conversation_id", sa.UUID(), nullable=True))
    # Cheap here specifically because it runs before either backfill pass —
    # every conversation_id is still NULL, which trivially satisfies the FK,
    # so there is nothing to validate against `conversations` yet. See the
    # module docstring for why the index below is NOT created here too.
    op.create_foreign_key(
        "fk_runs_conversation_id_conversations",
        "runs",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    _backfill_conversation_id()
    # Matches Run.__table_args__ in tret/db/models.py — the composite the
    # spend-by-conversation rollup (api/analytics.py) actually filters and
    # groups on, not just a bare index on the new column. Created AFTER the
    # backfill above, not alongside the FK: building it first would have made
    # every batch's UPDATEs also maintain a brand-new index as they wrote.
    op.create_index(
        "runs_project_conversation_created",
        "runs",
        ["project_id", "conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("runs_project_conversation_created", table_name="runs")
    op.drop_constraint("fk_runs_conversation_id_conversations", "runs", type_="foreignkey")
    op.drop_column("runs", "conversation_id")
