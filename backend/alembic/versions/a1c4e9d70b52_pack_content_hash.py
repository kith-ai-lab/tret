"""packs.content_hash (pack integrity pinning)

Revision ID: a1c4e9d70b52
Revises: f18e4f74fc33
Create Date: 2026-08-04 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1c4e9d70b52'
down_revision = 'f18e4f74fc33'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: packs installed before pinning keep working and are re-pinned on
    # their next (idempotent) install.
    op.add_column('packs', sa.Column('content_hash', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('packs', 'content_hash')
