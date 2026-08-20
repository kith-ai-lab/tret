"""runs.model_timeline — every model a run used, and what each one spent

Revision ID: d8b3c05fa412
Revises: c4f7a1b23e69
Create Date: 2026-08-20 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd8b3c05fa412'
down_revision = 'c4f7a1b23e69'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, and never backfilled: a run that predates mid-run switching used
    # exactly one model, and `model_used` already says which. Writing a
    # synthetic single-entry timeline for those would claim a record that was
    # never kept.
    op.add_column(
        'runs',
        sa.Column('model_timeline', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('runs', 'model_timeline')
