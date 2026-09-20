"""runs.delegated_cost_usd — spend this run has caused through delegation

Revision ID: 9d3b1f6c7a2e
Revises: b8b354463da7
Create Date: 2026-09-20 00:00:00.000000

NOT NULL with server_default "0": every run that predates delegated-cost
tracking, delegated or not, genuinely caused zero delegated spend as far as
this column is concerned (the engine never rolled a child's cost into its
parent before this change), so backfilling to 0 is the correct historical
value, not a placeholder — same shape as d2b7f5a91c34's cache token columns.
"""
from alembic import op
import sqlalchemy as sa

revision = "9d3b1f6c7a2e"
down_revision = "b8b354463da7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "delegated_cost_usd",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "delegated_cost_usd")
