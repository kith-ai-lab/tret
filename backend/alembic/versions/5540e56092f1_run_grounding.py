"""runs.grounding — the prose grounding check's verdict on a chat/freeform reply

Revision ID: 5540e56092f1
Revises: 7a132857be30
Create Date: 2026-09-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '5540e56092f1'
down_revision = '7a132857be30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable and never backfilled: the check is new, so a run recorded
    # before this genuinely was never checked, and null says exactly that —
    # a status of "clean" would falsely claim a run this migration cannot
    # retroactively verify.
    op.add_column(
        'runs',
        sa.Column('grounding', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('runs', 'grounding')
