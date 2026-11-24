"""company_classifications: peer taxonomy

Revision ID: c4a7e2b9d158
Revises: b6e1d9a3c472
Create Date: 2026-11-23 18:00:00.000000

- `company_classifications`: one row per (workspace, company key, source) holding the
  company's NACE Rev. 2 ladder (group, division, cluster, section) from one source
  (`sec_sic`, `extractor`, `manual`). Written by `tret disclosure classify-sec` and the
  manual override route; the effective classification is manual > sec_sic > extractor.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c4a7e2b9d158'
down_revision = 'b6e1d9a3c472'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'company_classifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'workspace_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('company_key_type', sa.Text(), nullable=False),
        sa.Column('company_key', sa.Text(), nullable=False),
        sa.Column('company_name', sa.Text(), nullable=True),
        sa.Column('nace_group', sa.Text(), nullable=True),
        sa.Column('nace_division', sa.Text(), nullable=False),
        sa.Column('cluster', sa.Text(), nullable=False),
        sa.Column('nace_section', sa.Text(), nullable=False),
        sa.Column('taxonomy_version', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('source_code', sa.Text(), nullable=True),
        sa.Column('source_label', sa.Text(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('match', sa.Text(), nullable=True),
        sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('gics_sector', sa.Text(), nullable=True),
        sa.Column('gics_disagrees', sa.Boolean(), nullable=True),
        sa.Column('reviewed_by', sa.Text(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('workspace_id', 'company_key_type', 'company_key', 'source'),
    )
    op.create_index(
        'ix_company_classifications_ws_division', 'company_classifications',
        ['workspace_id', 'nace_division'],
    )
    op.create_index(
        'ix_company_classifications_ws_cluster', 'company_classifications',
        ['workspace_id', 'cluster'],
    )


def downgrade() -> None:
    op.drop_index('ix_company_classifications_ws_cluster', table_name='company_classifications')
    op.drop_index('ix_company_classifications_ws_division', table_name='company_classifications')
    op.drop_table('company_classifications')
