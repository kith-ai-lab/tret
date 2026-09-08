"""connection_activity table

Revision ID: d65064395ad0
Revises: 63e11dc72bd7
Create Date: 2026-09-08 13:15:01.034398
"""
from alembic import op
import sqlalchemy as sa

revision = 'd65064395ad0'
down_revision = '63e11dc72bd7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'connection_activity',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('actor_user_id', sa.UUID(), nullable=True),
        sa.Column('actor_run_id', sa.UUID(), nullable=True),
        sa.Column('target', sa.Text(), nullable=True),
        sa.Column('bytes', sa.BigInteger(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['actor_run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_connection_activity_workspace_id', 'connection_activity', ['workspace_id']
    )
    op.create_index(
        'ix_connection_activity_workspace_created',
        'connection_activity',
        ['workspace_id', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_connection_activity_workspace_created', table_name='connection_activity')
    op.drop_index('ix_connection_activity_workspace_id', table_name='connection_activity')
    op.drop_table('connection_activity')
