"""disclosure_reports: Stage 1 normalised reports

Revision ID: b6e1d9a3c472
Revises: f4c1a8e93d27
Create Date: 2026-11-23 12:00:00.000000

- `disclosure_reports`: one row per (document, normaliser version) holding the
  quality metrics, the quarantine status and the blob-store key of the report's
  normalised Markdown (the Markdown itself is never in Postgres). Written only
  when `TRET_DISCLOSURE_NORMALISER=on`; the table is empty otherwise.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b6e1d9a3c472'
down_revision = 'f4c1a8e93d27'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'disclosure_reports',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column(
            'document_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column(
            'batch_target_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('collection_targets.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.Column(
            'run_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('runs.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('source_kind', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('quarantine_reason', sa.Text(), nullable=True),
        sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('markdown_key', sa.Text(), nullable=True),
        sa.Column('markdown_sha256', sa.Text(), nullable=True),
        sa.Column('markdown_chars', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('normaliser_version', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('document_id', 'normaliser_version'),
    )
    op.create_index('ix_disclosure_reports_batch_target_id', 'disclosure_reports', ['batch_target_id'])
    op.create_index('ix_disclosure_reports_project_id', 'disclosure_reports', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_disclosure_reports_project_id', table_name='disclosure_reports')
    op.drop_index('ix_disclosure_reports_batch_target_id', table_name='disclosure_reports')
    op.drop_table('disclosure_reports')
