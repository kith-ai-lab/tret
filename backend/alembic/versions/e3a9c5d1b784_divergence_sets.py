"""divergence sets and spot checks

Revision ID: e3a9c5d1b784
Revises: d7b3f1a9c542
Create Date: 2026-11-25 18:00:00.000000

PR 9 of the disclosure evaluation engine: `divergence_sets` (one completed Credibility pass joined with
1-4 completed tournaments, the join frozen in `computed`, the gates acknowledged on it) and `spot_checks`
(the human triads and critiques of a set, shaped like approvals with purpose `spot_check`). Additions
only: nothing existing changes.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e3a9c5d1b784'
down_revision = 'd7b3f1a9c542'
branch_labels = None
depends_on = None


def _uuid():
    return postgresql.UUID(as_uuid=True)


def _jsonb():
    return postgresql.JSONB(astext_type=sa.Text())


def _fk(target: str, ondelete: str = 'CASCADE'):
    return sa.ForeignKey(target, ondelete=ondelete)


def _now():
    return sa.text('now()')


def upgrade() -> None:
    op.create_table(
        'divergence_sets',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('workspace_id', _uuid(), _fk('workspaces.id'), nullable=False),
        sa.Column('project_id', _uuid(), _fk('projects.id'), nullable=False),
        sa.Column('name', sa.Text(), nullable=False, server_default=''),
        sa.Column('credibility_pass_id', _uuid(), _fk('credibility_passes.id'), nullable=False),
        sa.Column('tournament_ids', _jsonb(), nullable=False, server_default='[]'),
        sa.Column('modules', _jsonb(), nullable=False, server_default='[]'),
        sa.Column('arm_key', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='open'),
        sa.Column('computed', _jsonb(), nullable=False, server_default='{}'),
        sa.Column('acknowledged_gates', _jsonb(), nullable=False, server_default='[]'),
        sa.Column('created_by', _uuid(), _fk('users.id', 'SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column('reviewed_by', _uuid(), _fk('users.id', 'SET NULL'), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_divergence_sets_ws_created', 'divergence_sets', ['workspace_id', 'created_at'])
    op.create_table(
        'spot_checks',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('set_id', _uuid(), _fk('divergence_sets.id'), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('purpose', sa.Text(), nullable=False, server_default='spot_check'),
        sa.Column('item', _jsonb(), nullable=False, server_default='{}'),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('answer', _jsonb(), nullable=True),
        sa.Column('agreement', sa.Float(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('decided_by', _uuid(), _fk('users.id', 'SET NULL'), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('set_id', 'kind', 'position'),
    )
    op.create_index('ix_spot_checks_set', 'spot_checks', ['set_id'])


def downgrade() -> None:
    op.drop_index('ix_spot_checks_set', table_name='spot_checks')
    op.drop_table('spot_checks')
    op.drop_index('ix_divergence_sets_ws_created', table_name='divergence_sets')
    op.drop_table('divergence_sets')
