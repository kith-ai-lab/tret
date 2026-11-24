"""routing_freezes

Revision ID: a9d4e7c1f352
Revises: e7a4c9d2b861
Create Date: 2026-11-24 18:00:00.000000

- `routing_freezes`: the model selection frozen for one measurement pass (the jury of the
  evaluation engine, or the one model of its sealed call), with the full selection and the reason
  for every pick. Unique per (workspace, purpose, pass_ref). Empty until a freeze is made.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a9d4e7c1f352'
down_revision = 'e7a4c9d2b861'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'routing_freezes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'workspace_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('purpose', sa.Text(), nullable=False),
        sa.Column('pass_ref', sa.Text(), nullable=False),
        sa.Column('model_policy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('k', sa.Integer(), nullable=False),
        sa.Column('selection', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='active'),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('workspace_id', 'purpose', 'pass_ref'),
    )
    op.create_index('ix_routing_freezes_ws', 'routing_freezes', ['workspace_id'])


def downgrade() -> None:
    op.drop_index('ix_routing_freezes_ws', table_name='routing_freezes')
    op.drop_table('routing_freezes')
