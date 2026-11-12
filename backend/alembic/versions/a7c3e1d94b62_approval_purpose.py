"""approvals purpose

Revision ID: a7c3e1d94b62
Revises: f2a9c6d4e817
Create Date: 2026-11-12 18:00:00.000000

`purpose` says why a decision was made: 'review' (a verdict on the output, quality
evidence for the run's model) or 'packaging' (a page map approved only so it can
be packaged; not evidence). Existing rows default to 'review'.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7c3e1d94b62'
down_revision = 'f2a9c6d4e817'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'approvals',
        sa.Column('purpose', sa.Text(), nullable=False, server_default='review'),
    )


def downgrade() -> None:
    op.drop_column('approvals', 'purpose')
