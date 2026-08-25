"""invites table

Revision ID: e3c974913c19
Revises: 8eef9d61c7c4
Create Date: 2026-08-25 09:15:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'e3c974913c19'
down_revision = '8eef9d61c7c4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'invites',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('email', sa.Text(), nullable=False),
        sa.Column('role', sa.Text(), nullable=False, server_default='analyst'),
        sa.Column('token', sa.Text(), nullable=False),
        sa.Column('invited_by', sa.UUID(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['invited_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token'),
    )
    op.create_index('ix_invites_workspace_id', 'invites', ['workspace_id'])
    op.create_index('ix_invites_email', 'invites', ['email'])


def downgrade() -> None:
    op.drop_index('ix_invites_email', table_name='invites')
    op.drop_index('ix_invites_workspace_id', table_name='invites')
    op.drop_table('invites')
