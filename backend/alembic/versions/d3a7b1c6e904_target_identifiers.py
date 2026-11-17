"""register target identifiers

Revision ID: d3a7b1c6e904
Revises: c8d2f5a1b9e3
Create Date: 2026-11-16 09:00:00.000000

`collection_targets.identifiers`: the register's optional public company identifiers
(`lei`, `cik`, `ticker`, `source_filing_url`), only the keys that were given; null when none.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd3a7b1c6e904'
down_revision = 'c8d2f5a1b9e3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'collection_targets',
        sa.Column('identifiers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('collection_targets', 'identifiers')
