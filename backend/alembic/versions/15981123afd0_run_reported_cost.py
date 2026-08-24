"""runs.reported_cost_usd (provider-reported actuals, OpenRouter today)

Revision ID: 15981123afd0
Revises: f6c02d1948ab
Create Date: 2026-08-24 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '15981123afd0'
down_revision = 'f6c02d1948ab'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, no backfill: runs predating this carry no provider-reported
    # actual, and a zero would claim they cost nothing rather than that nothing
    # was recorded. `cost_usd` (unchanged) remains the catalog-priced figure
    # used for routing and cost caps.
    op.add_column(
        'runs',
        sa.Column('reported_cost_usd', sa.Numeric(12, 6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('runs', 'reported_cost_usd')
