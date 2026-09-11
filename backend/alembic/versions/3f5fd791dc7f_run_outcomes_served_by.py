"""run_outcomes.served_by — which upstream endpoint served this segment

Revision ID: 3f5fd791dc7f
Revises: 6b018767adc6
Create Date: 2026-09-10 22:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '3f5fd791dc7f'
down_revision = '6b018767adc6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, never backfilled: only OpenRouter (`openrouter_metadata`) and
    # Anthropic direct (the constant `SERVED_BY = "anthropic"` on every
    # `TurnComplete`, see providers/anthropic.py) report a serving upstream
    # today — see engine/harness.py ModelSegment.served_by — so only a local
    # run or a run older than this column genuinely has no answer, not an
    # absent one worth guessing.
    op.add_column('run_outcomes', sa.Column('served_by', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('run_outcomes', 'served_by')
