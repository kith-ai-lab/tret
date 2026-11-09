"""batch_evaluations and target_evaluations tables

Revision ID: b2f6d8a31c57
Revises: e3a7c5d91b04
Create Date: 2026-11-06 14:00:00.000000

The batch evaluator (`tret/batches/evaluation.py`): one pass per batch scores
each settled target against a frozen rubric with a fixed judge model, and keeps
the pass-level review, the per-target scores and the human spot checks.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b2f6d8a31c57'
down_revision = 'e3a7c5d91b04'
branch_labels = None
depends_on = None


def _jsonb(name: str, *, nullable: bool, default: str | None = None) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), nullable=nullable,
        server_default=default,
    )


def upgrade() -> None:
    op.create_table(
        'batch_evaluations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('collection_id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('family_key', sa.Text(), nullable=False),
        sa.Column('rubric_key', sa.Text(), nullable=False),
        sa.Column('rubric_version', sa.Text(), nullable=False),
        _jsonb('rubric', nullable=False, default='{}'),
        sa.Column('judge_model', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='running'),
        sa.Column('budget_usd', sa.Numeric(10, 4), nullable=True),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        _jsonb('summary', nullable=True),
        sa.Column('summary_run_id', sa.UUID(), nullable=True),
        _jsonb('booked_runs', nullable=False, default='[]'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('finished_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.ForeignKeyConstraint(['summary_run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_batch_evaluations_collection_created', 'batch_evaluations',
        ['collection_id', 'created_at'], unique=False,
    )
    op.create_index(
        'uq_batch_evaluations_one_running', 'batch_evaluations', ['collection_id'],
        unique=True, postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        'target_evaluations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('evaluation_id', sa.UUID(), nullable=False),
        sa.Column('collection_id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('target_id', sa.UUID(), nullable=False),
        sa.Column('row_key', sa.Text(), nullable=False),
        sa.Column('rubric_key', sa.Text(), nullable=False),
        sa.Column('family_key', sa.Text(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('evaluated_run_id', sa.UUID(), nullable=True),
        sa.Column('evaluator_run_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('skip_reason', sa.Text(), nullable=True),
        _jsonb('scores', nullable=False, default='{}'),
        sa.Column('quality', sa.Numeric(5, 2), nullable=True),
        sa.Column('difficulty', sa.SmallInteger(), nullable=True),
        _jsonb('flags', nullable=False, default='[]'),
        sa.Column('confidence', sa.Text(), nullable=True),
        sa.Column('rationale', sa.Text(), nullable=True),
        _jsonb('relative', nullable=True),
        _jsonb('review', nullable=True),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('finished_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['evaluation_id'], ['batch_evaluations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.ForeignKeyConstraint(['target_id'], ['collection_targets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['evaluated_run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['evaluator_run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('evaluation_id', 'target_id'),
    )
    op.create_index(
        'ix_target_evaluations_evaluation_id', 'target_evaluations', ['evaluation_id'], unique=False
    )
    op.create_index('ix_target_evaluations_target_id', 'target_evaluations', ['target_id'], unique=False)
    op.create_index(
        'ix_target_evaluations_evaluator_run_id', 'target_evaluations', ['evaluator_run_id'], unique=False
    )
    op.create_index(
        'ix_target_evaluations_history', 'target_evaluations',
        ['workspace_id', 'row_key', 'rubric_key', 'family_key'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_target_evaluations_history', table_name='target_evaluations')
    op.drop_index('ix_target_evaluations_evaluator_run_id', table_name='target_evaluations')
    op.drop_index('ix_target_evaluations_target_id', table_name='target_evaluations')
    op.drop_index('ix_target_evaluations_evaluation_id', table_name='target_evaluations')
    op.drop_table('target_evaluations')
    op.drop_index('uq_batch_evaluations_one_running', table_name='batch_evaluations')
    op.drop_index('ix_batch_evaluations_collection_created', table_name='batch_evaluations')
    op.drop_table('batch_evaluations')
