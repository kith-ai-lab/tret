"""run_outcomes task_family and evaluated_quality_score

Revision ID: f2a9c6d4e817
Revises: e5b8c2d7f1a3
Create Date: 2026-11-12 16:00:00.000000

`task_family` (`<family_key>@<workspace_id>`, null for non-batch runs) and
`evaluated_quality_score` (the outcome score blended with the batch evaluator's
score; null when there is no usable evaluation) feed per-workspace, gate-protected
family priors for the router.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a9c6d4e817'
down_revision = 'e5b8c2d7f1a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('run_outcomes', sa.Column('task_family', sa.Text(), nullable=True))
    op.add_column('run_outcomes', sa.Column('evaluated_quality_score', sa.Numeric(6, 4), nullable=True))
    op.create_index(
        'run_outcomes_family', 'run_outcomes', ['task_family', 'model_id'],
        postgresql_where=sa.text('task_family IS NOT NULL'),
        sqlite_where=sa.text('task_family IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('run_outcomes_family', table_name='run_outcomes')
    op.drop_column('run_outcomes', 'evaluated_quality_score')
    op.drop_column('run_outcomes', 'task_family')
