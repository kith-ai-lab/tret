"""batch experiments, target arm, harness policy history

Revision ID: e5b8c2d7f1a3
Revises: d9e4b7a1c2f8
Create Date: 2026-11-12 14:00:00.000000

`batch_experiments` (a person-started comparison of arms over one batch family),
`collection_targets.arm` (which arm a row of a split experiment runs under) and
`harness_policy_history` (who changed a harness's model policy / loop config, when,
and what it was before).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e5b8c2d7f1a3'
down_revision = 'd9e4b7a1c2f8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'batch_experiments',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('mode', sa.Text(), nullable=False),
        sa.Column('profile', sa.Text(), nullable=False),
        sa.Column('family_key', sa.Text(), nullable=False),
        sa.Column('harness_id', sa.UUID(), nullable=True),
        sa.Column('arms', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('sample_n', sa.Integer(), nullable=True),
        sa.Column('promoted', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['harness_id'], ['harnesses.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_batch_experiments_workspace_created', 'batch_experiments',
        ['workspace_id', 'created_at'], unique=False,
    )
    op.add_column('collection_targets', sa.Column('arm', sa.Text(), nullable=True))
    op.create_table(
        'harness_policy_history',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('harness_id', sa.UUID(), nullable=False),
        sa.Column('changed_by', sa.UUID(), nullable=True),
        sa.Column('changed_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('source_id', sa.UUID(), nullable=True),
        sa.Column('previous_model_policy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('previous_loop_config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('new_model_policy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('new_loop_config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['harness_id'], ['harnesses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_harness_policy_history_harness_changed', 'harness_policy_history',
        ['harness_id', 'changed_at'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_harness_policy_history_harness_changed', table_name='harness_policy_history')
    op.drop_table('harness_policy_history')
    op.drop_column('collection_targets', 'arm')
    op.drop_index('ix_batch_experiments_workspace_created', table_name='batch_experiments')
    op.drop_table('batch_experiments')
