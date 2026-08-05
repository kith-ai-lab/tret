"""runs.energy_wh + runs.energy_accounting (estimated energy/carbon per run)

Revision ID: f4c1d8ab26e7
Revises: e5a71c0b93df
Create Date: 2026-08-04 22:30:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f4c1d8ab26e7'
down_revision = 'e5a71c0b93df'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both nullable, and deliberately not defaulted to 0: runs that predate
    # ecological accounting have no estimate, and a zero would read as "this run
    # drew no power". Backfilling is not possible either — the estimate depends
    # on the chosen model's energy class, and the catalog moves.
    op.add_column('runs', sa.Column('energy_wh', sa.Numeric(14, 6), nullable=True))
    op.add_column(
        'runs',
        sa.Column('energy_accounting', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('runs', 'energy_accounting')
    op.drop_column('runs', 'energy_wh')
