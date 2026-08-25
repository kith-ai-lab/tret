"""draft packs table

Revision ID: 82169de695f6
Revises: e3c974913c19
Create Date: 2026-08-25 15:40:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '82169de695f6'
down_revision = 'e3c974913c19'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'draft_packs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('slug', sa.Text(), nullable=False),
        sa.Column('manifest_json', postgresql.JSONB(), nullable=False),
        sa.Column('files', postgresql.JSONB(), nullable=False),
        sa.Column('test_install_seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_draft_packs_workspace_id', 'draft_packs', ['workspace_id'])


def downgrade() -> None:
    op.drop_index('ix_draft_packs_workspace_id', table_name='draft_packs')
    op.drop_table('draft_packs')
