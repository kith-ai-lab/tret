"""egress_calls table + documents.source_kind

Revision ID: b3e9f21d5c47
Revises: f4c1d8ab26e7
Create Date: 2026-08-20 09:12:04.118902
"""
from alembic import op
import sqlalchemy as sa

revision = 'b3e9f21d5c47'
down_revision = 'f4c1d8ab26e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'egress_calls',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=True),
        sa.Column('project_id', sa.UUID(), nullable=True),
        sa.Column('egress_class', sa.Text(), nullable=False),
        sa.Column('method', sa.Text(), nullable=False),
        sa.Column('host', sa.Text(), nullable=False),
        sa.Column('path', sa.Text(), nullable=False, server_default=''),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('byte_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('duration_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('decision', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_egress_calls_created_at', 'egress_calls', ['created_at'])
    # Existing documents were all put there by a person. The server default backfills
    # them and stays on the column: a row written by code that predates this column
    # should read as an upload, never as web-sourced evidence.
    op.add_column(
        'documents',
        sa.Column('source_kind', sa.Text(), nullable=False, server_default='upload'),
    )


def downgrade() -> None:
    op.drop_column('documents', 'source_kind')
    op.drop_index('ix_egress_calls_created_at', table_name='egress_calls')
    op.drop_table('egress_calls')
