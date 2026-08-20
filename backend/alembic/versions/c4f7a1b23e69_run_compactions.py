"""runs.compactions — the record of what a run had to elide to keep going

Revision ID: c4f7a1b23e69
Revises: b3e2f90a4c17
Create Date: 2026-08-20 11:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c4f7a1b23e69'
down_revision = 'b3e2f90a4c17'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable rather than defaulted to []: a run that predates compaction never
    # faced the question, which is not the same as a run that faced it and did
    # nothing. `runs.messages` remains the complete transcript in both cases —
    # this column records only what the provider stopped being shown.
    op.add_column(
        'runs',
        sa.Column('compactions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('runs', 'compactions')
