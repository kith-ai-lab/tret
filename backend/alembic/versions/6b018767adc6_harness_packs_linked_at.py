"""harnesses.packs_linked_at (one-time marker for the chat-harness default pack link)

Revision ID: 6b018767adc6
Revises: d65064395ad0
Create Date: 2026-09-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '6b018767adc6'
down_revision = 'd65064395ad0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, no backfill: a NULL means "the chat-harness default pack link
    # has never been applied to this harness" (services.workspace's
    # _seed_chat_harness backfills it exactly once), which is true of every
    # row that already exists at the time this migration runs, chat harness
    # or not — the next boot's seeding pass is what sets it.
    op.add_column(
        'harnesses',
        sa.Column('packs_linked_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('harnesses', 'packs_linked_at')
