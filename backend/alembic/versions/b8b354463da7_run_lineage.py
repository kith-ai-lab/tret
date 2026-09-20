"""runs delegation lineage — parent_run_id, root_run_id, delegation_kind,
delegation_batch_id

Revision ID: b8b354463da7
Revises: 9ba228f09f91
Create Date: 2026-09-20 00:00:00.000000

Four nullable columns, no backfill: `run_harness_task` (engine/tools.py) has
so far kept parent→child lineage in an in-memory dict on the engine only, so
there is nothing in any existing row to backfill these from — every run that
predates this migration, delegated or not, genuinely has no lineage recorded
and stays null. A later change teaches the engine to set these columns on a
freshly-created child run; this migration only adds the place for it to write
to.

Two self-referential FKs (`parent_run_id`, `root_run_id`, both -> `runs.id`),
both `ondelete="SET NULL"` for the same reason `runs.conversation_id` (see
that migration) points `SET NULL` at a deletable parent: nothing deletes a
Run today, but a delegated run outliving the ancestor that caused it is the
same "not delegated from anything live" state as never having had one, and
deleting an ancestor must not cascade into deleting — or being blocked from
deleting — the runs it spawned.

Both FKs are created before either column has ever been written (this is a
brand-new column, so every row's value is NULL at the moment the constraint
is added) — NULL trivially satisfies a foreign key, so, exactly as
9ba228f09f91 notes for `conversation_id`, there is nothing for the validating
scan to check yet and no backfill-ordering trick is needed here.
"""
from alembic import op
import sqlalchemy as sa

revision = "b8b354463da7"
down_revision = "9ba228f09f91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("parent_run_id", sa.UUID(), nullable=True))
    op.add_column("runs", sa.Column("root_run_id", sa.UUID(), nullable=True))
    op.add_column("runs", sa.Column("delegation_kind", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("delegation_batch_id", sa.UUID(), nullable=True))

    op.create_foreign_key(
        "fk_runs_parent_run_id_runs",
        "runs",
        "runs",
        ["parent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_runs_root_run_id_runs",
        "runs",
        "runs",
        ["root_run_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Postgres never indexes a foreign-key column automatically — the FKs
    # above only enforce the constraint, they buy no query speed. Both
    # columns are the join key for "this run's whole delegation tree" (see
    # api/runs.py's `/{run_id}/children`, which filters on `parent_run_id`,
    # and the `tree` block on run detail, which filters on `id == root OR
    # root_run_id == root`), so both need an explicit index of their own.
    op.create_index("ix_runs_parent_run_id", "runs", ["parent_run_id"])
    op.create_index("ix_runs_root_run_id", "runs", ["root_run_id"])


def downgrade() -> None:
    op.drop_index("ix_runs_root_run_id", table_name="runs")
    op.drop_index("ix_runs_parent_run_id", table_name="runs")
    op.drop_constraint("fk_runs_root_run_id_runs", "runs", type_="foreignkey")
    op.drop_constraint("fk_runs_parent_run_id_runs", "runs", type_="foreignkey")
    op.drop_column("runs", "delegation_batch_id")
    op.drop_column("runs", "delegation_kind")
    op.drop_column("runs", "root_run_id")
    op.drop_column("runs", "parent_run_id")
