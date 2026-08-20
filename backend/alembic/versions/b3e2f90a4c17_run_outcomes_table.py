"""run_outcomes: per-run routing evidence (derived, rebuildable)

Revision ID: b3e2f90a4c17
Revises: b3e9f21d5c47
Create Date: 2026-08-20 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b3e2f90a4c17'
down_revision = 'b3e9f21d5c47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every column here is derived from runs/findings/approvals, so this table is
    # safe to drop and rebuild (`tret outcomes backfill`) — which is also why
    # the FK cascades: an outcome without its run is not evidence of anything.
    op.create_table(
        'run_outcomes',
        sa.Column('run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('harness_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('task_type', sa.Text(), nullable=False),
        sa.Column('task_shape', sa.Text(), nullable=False),
        sa.Column('objective', sa.Text(), nullable=False),
        sa.Column('max_cost_tier', sa.Text(), nullable=False),
        sa.Column('size_band', sa.Text(), nullable=False),
        sa.Column('model_id', sa.Text(), nullable=False),
        sa.Column('provider', sa.Text(), nullable=True),
        sa.Column('fallback_used', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('override', sa.Text(), nullable=True),
        sa.Column('outcome_class', sa.Text(), nullable=False),
        sa.Column('quality_score', sa.Numeric(6, 4), nullable=False),
        sa.Column('score_version', sa.Text(), nullable=False),
        sa.Column('error_kind', sa.Text(), nullable=True),
        sa.Column('components', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column('iterations', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('input_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.BigInteger(), nullable=False, server_default='0'),
        # Nullable, never 0 — same reasoning as runs.energy_wh: a run with no
        # estimate has no figure, and a zero would read as "drew no power".
        sa.Column('energy_wh', sa.Numeric(14, 6), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('findings_created', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('findings_approved', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('findings_rejected', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('observed_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['harness_id'], ['harnesses.id']),
        sa.PrimaryKeyConstraint('run_id'),
    )
    # The tuple the priors group by, and the recency filter they apply to it.
    op.create_index(
        'run_outcomes_routing_key', 'run_outcomes', ['task_shape', 'objective', 'model_id']
    )
    op.create_index('run_outcomes_observed', 'run_outcomes', ['observed_at'])


def downgrade() -> None:
    op.drop_index('run_outcomes_observed', table_name='run_outcomes')
    op.drop_index('run_outcomes_routing_key', table_name='run_outcomes')
    op.drop_table('run_outcomes')
