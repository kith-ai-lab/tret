"""report_modules claim density, extraction_reviews

Revision ID: e7a4c9d2b861
Revises: d8f3a1c5b7e2
Create Date: 2026-11-24 14:00:00.000000

- `report_modules.claim_count`, `claim_density` (claims per 100 words), `claim_counts` (by
  category) and `density_version`: the deterministic claim counter of
  `tret/evaluation/density.py`, filled by the extractor and by `tret disclosure claim-density`.
  All nullable: absent modules and rows from before this revision stay null.
- `extraction_reviews`: a reviewer's decision (accepted | dismissed | fixed) on one computed
  review item of an extraction, keyed by a stable `item_key`, so a decided item does not come
  back. Empty until a reviewer decides something.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e7a4c9d2b861'
down_revision = 'd8f3a1c5b7e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('report_modules', sa.Column('claim_count', sa.Integer(), nullable=True))
    op.add_column('report_modules', sa.Column('claim_density', sa.Float(), nullable=True))
    op.add_column('report_modules', sa.Column('claim_counts', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('report_modules', sa.Column('density_version', sa.Text(), nullable=True))
    op.create_table(
        'extraction_reviews',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'workspace_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column(
            'extraction_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('report_extractions.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('item_key', sa.Text(), nullable=False),
        sa.Column('decision', sa.Text(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.Text(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('extraction_id', 'item_key'),
    )
    op.create_index('ix_extraction_reviews_ws', 'extraction_reviews', ['workspace_id'])


def downgrade() -> None:
    op.drop_index('ix_extraction_reviews_ws', table_name='extraction_reviews')
    op.drop_table('extraction_reviews')
    op.drop_column('report_modules', 'density_version')
    op.drop_column('report_modules', 'claim_counts')
    op.drop_column('report_modules', 'claim_density')
    op.drop_column('report_modules', 'claim_count')
