"""collections and collection_targets tables

Revision ID: c4e9a2f71b38
Revises: a7d3e8c1f905
Create Date: 2026-11-05 10:00:00.000000

A collection run (`tret/services/collections.py`): one register of target
companies, each collected by its own engine run and tracked through approval,
packaging and SharePoint upload.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c4e9a2f71b38'
down_revision = 'a7d3e8c1f905'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'collections',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('run_slug', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='draft'),
        sa.Column(
            'settings', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='{}',
        ),
        sa.Column(
            'warnings', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='[]',
        ),
        sa.Column('finalize', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('started_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('finalized_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('workspace_id', 'run_slug'),
    )
    op.create_index(
        'ix_collections_workspace_created', 'collections', ['workspace_id', 'created_at'],
        unique=False,
    )
    op.create_table(
        'collection_targets',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('collection_id', sa.UUID(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('slug', sa.Text(), nullable=False),
        sa.Column('company', sa.Text(), nullable=False),
        sa.Column('market', sa.Text(), nullable=False),
        sa.Column('domicile_country', sa.Text(), nullable=True),
        sa.Column('sector', sa.Text(), nullable=True),
        sa.Column('category', sa.Text(), nullable=True),
        sa.Column('expected_form', sa.Text(), nullable=False, server_default='unknown'),
        sa.Column('fiscal_year', sa.Integer(), nullable=False),
        sa.Column(
            'known_urls', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='[]',
        ),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('run_id', sa.UUID(), nullable=True),
        sa.Column(
            'finding_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='[]',
        ),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('outcome', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'written_files', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='[]',
        ),
        sa.Column(
            'history', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='[]',
        ),
        sa.Column('started_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('finished_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('collection_id', 'slug'),
    )
    op.create_index(
        'ix_collection_targets_collection_position', 'collection_targets',
        ['collection_id', 'position'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_collection_targets_collection_position', table_name='collection_targets')
    op.drop_table('collection_targets')
    op.drop_index('ix_collections_workspace_created', table_name='collections')
    op.drop_table('collections')
