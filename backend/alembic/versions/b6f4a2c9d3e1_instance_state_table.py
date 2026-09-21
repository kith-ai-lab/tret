"""instance_state table — the first instance-level key/value store

Revision ID: b6f4a2c9d3e1
Revises: 9d3b1f6c7a2e
Create Date: 2026-09-21 00:00:00.000000

Introduced for opt-in telemetry (tret/services/telemetry.py): the admin's DB
toggle, the minted instance id, and the local record of the last 10 send
attempts all live here as separate keys, one row each.

Same-feature, same revision: N10's `runs_created_at` index. The telemetry
payload build (`build_payload`) scopes nearly every query to a bare
`created_at` window with no project/workspace to lead on, which none of the
existing `runs` composite indexes serve — without this, every sender-loop
tick was a full seq scan.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "b6f4a2c9d3e1"
down_revision = "9d3b1f6c7a2e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instance_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("runs_created_at", "runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("runs_created_at", table_name="runs")
    op.drop_table("instance_state")
