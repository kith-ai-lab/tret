"""workspace_connections table

Revision ID: 63e11dc72bd7
Revises: 84cf41ced91f
Create Date: 2026-08-31 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '63e11dc72bd7'
down_revision = '84cf41ced91f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'workspace_connections',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.Text(), nullable=False),
        sa.Column('account_label', sa.Text(), nullable=True),
        sa.Column('encrypted_refresh_token', sa.LargeBinary(), nullable=False),
        sa.Column(
            'granted_scopes', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='[]',
        ),
        sa.Column(
            'selected_resources', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='{}',
        ),
        sa.Column('status', sa.Text(), nullable=False, server_default='active'),
        sa.Column('error_detail', sa.Text(), nullable=True),
        sa.Column('connected_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('refreshed_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['connected_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('workspace_id', 'provider'),
    )


def downgrade() -> None:
    op.drop_table('workspace_connections')
