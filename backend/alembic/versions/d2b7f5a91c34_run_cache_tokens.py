"""runs.cache_read_tokens / cache_write_tokens (cache-aware cost accounting)

Revision ID: d2b7f5a91c34
Revises: a1c4e9d70b52
Create Date: 2026-08-04 11:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'd2b7f5a91c34'
down_revision = 'a1c4e9d70b52'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Runs that predate cache accounting report zero cached tokens; their
    # cost_usd was computed with everything in the uncached input bucket, which
    # is the correct historical number.
    op.add_column(
        'runs',
        sa.Column('cache_read_tokens', sa.BigInteger(), nullable=False, server_default='0'),
    )
    op.add_column(
        'runs',
        sa.Column('cache_write_tokens', sa.BigInteger(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('runs', 'cache_write_tokens')
    op.drop_column('runs', 'cache_read_tokens')
