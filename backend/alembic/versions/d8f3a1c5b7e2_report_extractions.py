"""report_extractions, report_modules: Stage 2 module extractor

Revision ID: d8f3a1c5b7e2
Revises: c4a7e2b9d158
Create Date: 2026-11-24 10:00:00.000000

- `report_extractions`: one row per (report, extractor version, module set) holding the
  merged metadata (document type, year, frameworks, assurance, NACE proposal, block-id
  evidence), the number of windows, the issues found while validating the model's answer
  and the status. Written by the `disclosure_extract` batch profile when
  `TRET_DISCLOSURE_EXTRACT=on`; empty otherwise.
- `report_modules`: one row per (extraction, module): present, the spans (block ids and
  pages), the blob-store key and sha256 of the verbatim text, its word count, confidence
  and whether it was cut to the module's `max_words`. The text is never in Postgres.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd8f3a1c5b7e2'
down_revision = 'c4a7e2b9d158'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'report_extractions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'workspace_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column(
            'disclosure_report_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('disclosure_reports.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column(
            'batch_target_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('collection_targets.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('runs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('extractor_version', sa.Text(), nullable=False),
        sa.Column('module_set', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('models', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('windows', sa.Integer(), nullable=False),
        sa.Column('issues', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('disclosure_report_id', 'extractor_version', 'module_set'),
    )
    op.create_index('ix_report_extractions_ws', 'report_extractions', ['workspace_id'])
    op.create_table(
        'report_modules',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'extraction_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('report_extractions.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column(
            'disclosure_report_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('disclosure_reports.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('module', sa.Text(), nullable=False),
        sa.Column('present', sa.Boolean(), nullable=False),
        sa.Column('spans', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('text_key', sa.Text(), nullable=True),
        sa.Column('text_sha256', sa.Text(), nullable=True),
        sa.Column('word_count', sa.Integer(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('truncated', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('extraction_id', 'module'),
    )
    op.create_index('ix_report_modules_report_module', 'report_modules', ['disclosure_report_id', 'module'])


def downgrade() -> None:
    op.drop_index('ix_report_modules_report_module', table_name='report_modules')
    op.drop_table('report_modules')
    op.drop_index('ix_report_extractions_ws', table_name='report_extractions')
    op.drop_table('report_extractions')
