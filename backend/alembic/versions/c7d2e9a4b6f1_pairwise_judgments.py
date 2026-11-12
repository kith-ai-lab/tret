"""pairwise_judgments table

Revision ID: c7d2e9a4b6f1
Revises: b2f6d8a31c57
Create Date: 2026-11-12 10:00:00.000000

The blind pairwise judge (`tret/batches/pairwise.py`): one row per register row
compared across two batches of a family, shown beside the rubric scores.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c7d2e9a4b6f1'
down_revision = 'b2f6d8a31c57'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'pairwise_judgments',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('family_key', sa.Text(), nullable=False),
        sa.Column('rubric_key', sa.Text(), nullable=False),
        sa.Column('rubric_version', sa.Text(), nullable=False),
        sa.Column('judge_model', sa.Text(), nullable=False),
        sa.Column('row_key', sa.Text(), nullable=False),
        sa.Column('batch_low_id', sa.UUID(), nullable=False),
        sa.Column('batch_high_id', sa.UUID(), nullable=False),
        sa.Column('target_low_id', sa.UUID(), nullable=False),
        sa.Column('target_high_id', sa.UUID(), nullable=False),
        sa.Column('attempt_low', sa.Integer(), nullable=True),
        sa.Column('attempt_high', sa.Integer(), nullable=True),
        sa.Column('run_low_id', sa.UUID(), nullable=True),
        sa.Column('run_high_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('verdict', sa.Text(), nullable=True),
        sa.Column('strength', sa.Text(), nullable=True),
        sa.Column('calls', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('budget_usd', sa.Numeric(10, 4), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('finished_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['batch_low_id'], ['collections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['batch_high_id'], ['collections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_low_id'], ['collection_targets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_high_id'], ['collection_targets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['run_low_id'], ['runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['run_high_id'], ['runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('target_low_id', 'target_high_id', 'judge_model', 'rubric_version'),
    )
    op.create_index(
        'ix_pairwise_judgments_batches', 'pairwise_judgments',
        ['workspace_id', 'batch_low_id', 'batch_high_id'], unique=False,
    )
    op.create_index(
        'ix_pairwise_judgments_target_high', 'pairwise_judgments', ['target_high_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_pairwise_judgments_target_high', table_name='pairwise_judgments')
    op.drop_index('ix_pairwise_judgments_batches', table_name='pairwise_judgments')
    op.drop_table('pairwise_judgments')
