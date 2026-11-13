"""batch shares

Revision ID: c8d2f5a1b9e3
Revises: a7c3e1d94b62
Create Date: 2026-11-13 09:00:00.000000

`batch_shares`: a read-only share of one batch with another workspace, addressed to a
person's email (pending until accepted from a workspace) or to a workspace the sharer
belongs to (active at once).
"""
from alembic import op
import sqlalchemy as sa

revision = 'c8d2f5a1b9e3'
down_revision = 'a7c3e1d94b62'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'batch_shares',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('batch_id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('recipient_email', sa.Text(), nullable=True),
        sa.Column('recipient_workspace_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('accepted_by', sa.UUID(), nullable=True),
        sa.Column('accepted_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['batch_id'], ['collections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recipient_workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['accepted_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_batch_shares_batch', 'batch_shares', ['batch_id'], unique=False)
    op.create_index(
        'ix_batch_shares_recipient_workspace', 'batch_shares',
        ['recipient_workspace_id', 'status'], unique=False,
    )
    op.create_index(
        'ix_batch_shares_recipient_email', 'batch_shares',
        ['recipient_email', 'status'], unique=False,
    )
    op.create_index(
        'uq_batch_shares_active', 'batch_shares',
        ['batch_id', 'recipient_workspace_id'], unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index('uq_batch_shares_active', table_name='batch_shares')
    op.drop_index('ix_batch_shares_recipient_email', table_name='batch_shares')
    op.drop_index('ix_batch_shares_recipient_workspace', table_name='batch_shares')
    op.drop_index('ix_batch_shares_batch', table_name='batch_shares')
    op.drop_table('batch_shares')
