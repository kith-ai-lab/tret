"""run_outcomes: one row per model a run used, not per run

Revision ID: e2a91f6b7c34
Revises: d8b3c05fa412
Create Date: 2026-08-20 13:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2a91f6b7c34'
down_revision = 'd8b3c05fa412'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A run that changed model produced evidence about every model it used, and
    # one row per run could only ever record the last of them. Existing rows are
    # all single-model runs, so they take segment 0 and keep their meaning
    # exactly.
    op.add_column(
        'run_outcomes',
        sa.Column('segment_index', sa.Integer(), nullable=False, server_default='0'),
    )
    op.drop_constraint('run_outcomes_pkey', 'run_outcomes', type_='primary')
    op.create_primary_key('run_outcomes_pkey', 'run_outcomes', ['run_id', 'segment_index'])


def downgrade() -> None:
    # Lossy by nature: a multi-model run has more rows than the old key can
    # hold, so the extra segments go. They are derived and rebuildable with
    # `bench outcomes backfill`.
    op.execute('DELETE FROM run_outcomes WHERE segment_index > 0')
    op.drop_constraint('run_outcomes_pkey', 'run_outcomes', type_='primary')
    op.create_primary_key('run_outcomes_pkey', 'run_outcomes', ['run_id'])
    op.drop_column('run_outcomes', 'segment_index')
