"""runs.overhead — the model calls a run makes about itself, finally metered

Revision ID: f6c02d1948ab
Revises: e2a91f6b7c34
Create Date: 2026-08-20 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f6c02d1948ab'
down_revision = 'e2a91f6b7c34'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable and never backfilled. Runs that predate this genuinely have no
    # figure — `complete_json` discarded the provider's usage block, so the
    # tokens are not recoverable from anything bench stored. A zero would claim
    # those runs did no routing, which is false for almost all of them.
    op.add_column(
        'runs',
        sa.Column('overhead', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('runs', 'overhead')
