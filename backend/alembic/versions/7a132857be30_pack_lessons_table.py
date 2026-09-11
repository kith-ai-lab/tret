"""pack_lessons table — per-workspace, per-pack-slug lessons memory

Keyed on (workspace_id, pack_slug, ordinal), not pack_id: a new pack version
is a new `packs` row, and re-keying on the id would silently drop a
workspace's lessons on every version bump. `pack_id` is kept as nullable
provenance (SET NULL on pack delete) rather than dropped outright, and rows
are never cascade-deleted when the pack version that produced them is.

Revision ID: 7a132857be30
Revises: 720520881b3e
Create Date: 2026-09-11 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "7a132857be30"
down_revision = "720520881b3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pack_lessons",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("pack_slug", sa.Text(), nullable=False),
        sa.Column("pack_id", sa.UUID(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("proposed_by_run_id", sa.UUID(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pack_id"], ["packs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["proposed_by_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "pack_slug", "ordinal", name="uq_pack_lessons_workspace_slug_ordinal"
        ),
    )
    op.create_index(
        "ix_pack_lessons_workspace_slug", "pack_lessons", ["workspace_id", "pack_slug"]
    )


def downgrade() -> None:
    op.drop_index("ix_pack_lessons_workspace_slug", table_name="pack_lessons")
    op.drop_table("pack_lessons")
