"""runs.context_composition (per-component estimated token breakdown)

Revision ID: e5a71c0b93df
Revises: d2b7f5a91c34
Create Date: 2026-08-04 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e5a71c0b93df'
down_revision = 'd2b7f5a91c34'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: runs that predate composition accounting simply have no
    # breakdown. Backfilling would mean re-assembling prompts against pack
    # versions that may since have changed, which would be a fiction.
    op.add_column(
        'runs',
        sa.Column('context_composition', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('runs', 'context_composition')
