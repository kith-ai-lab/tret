"""eval diagnostics and anchor sets

Revision ID: c5d1a8f3e726
Revises: b3f8c2a6d914
Create Date: 2026-11-25 09:00:00.000000

Engine 1 diagnostics (disclosure evaluation engine, PR 7): the gates and their per-round history on
`eval_tournaments` (`diagnostics`, `dropped_families`, `acknowledged_gates`), and the anchor sets of
the judge-drift gate (`eval_anchor_sets`). An `anchor` match has no pool members (its texts are the
anchor pair's), so `eval_matches.a_member_id` / `b_member_id` become nullable and the row carries
`anchor_set_id` + `anchor_index`. Additions only: existing rows are unchanged.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c5d1a8f3e726'
down_revision = 'b3f8c2a6d914'
branch_labels = None
depends_on = None


def _uuid():
    return postgresql.UUID(as_uuid=True)


def _jsonb():
    return postgresql.JSONB(astext_type=sa.Text())


def _now():
    return sa.text('now()')


def upgrade() -> None:
    op.add_column('eval_tournaments', sa.Column('diagnostics', _jsonb(), nullable=False, server_default='{}'))
    op.add_column('eval_tournaments', sa.Column('dropped_families', _jsonb(), nullable=False, server_default='[]'))
    op.add_column('eval_tournaments', sa.Column('acknowledged_gates', _jsonb(), nullable=False, server_default='{}'))
    op.create_table(
        'eval_anchor_sets',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('workspace_id', _uuid(), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('module', sa.Text(), nullable=False),
        sa.Column('module_set', sa.Text(), nullable=False),
        sa.Column('source_tournament_id', _uuid(), sa.ForeignKey('eval_tournaments.id', ondelete='SET NULL'), nullable=True),
        sa.Column('module_def', _jsonb(), nullable=False, server_default='{}'),
        sa.Column('judge_prompt_version', sa.Text(), nullable=True),
        sa.Column('freeze_id', _uuid(), sa.ForeignKey('routing_freezes.id', ondelete='SET NULL'), nullable=True),
        sa.Column('pairs', _jsonb(), nullable=False, server_default='[]'),
        sa.Column('status', sa.Text(), nullable=False, server_default='baseline_pending'),
        sa.Column('pause_reason', sa.Text(), nullable=True),
        sa.Column('budget_usd', sa.Numeric(12, 6), nullable=True),
        sa.Column('spent_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('lease_owner', sa.Text(), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index('ix_eval_anchor_sets_ws_module', 'eval_anchor_sets', ['workspace_id', 'module', 'module_set'])
    op.create_table(
        'eval_anchor_runs',
        sa.Column('run_id', _uuid(), primary_key=True),
        sa.Column('anchor_set_id', _uuid(), sa.ForeignKey('eval_anchor_sets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('pair_index', sa.Integer(), nullable=False),
        sa.Column('juror_index', sa.Integer(), nullable=False),
        sa.Column('family', sa.Text(), nullable=True),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('verdict', sa.Text(), nullable=True),
        sa.Column('strength', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('booked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index('ix_eval_anchor_runs_set', 'eval_anchor_runs', ['anchor_set_id'])
    op.alter_column('eval_matches', 'a_member_id', existing_type=_uuid(), nullable=True)
    op.alter_column('eval_matches', 'b_member_id', existing_type=_uuid(), nullable=True)
    op.add_column('eval_matches', sa.Column('anchor_set_id', _uuid(), sa.ForeignKey('eval_anchor_sets.id', ondelete='SET NULL'), nullable=True))
    op.add_column('eval_matches', sa.Column('anchor_index', sa.Integer(), nullable=True))
    op.create_unique_constraint(
        'uq_eval_matches_anchor', 'eval_matches', ['tournament_id', 'anchor_set_id', 'anchor_index', 'juror_index']
    )


def downgrade() -> None:
    op.drop_constraint('uq_eval_matches_anchor', 'eval_matches', type_='unique')
    op.execute("DELETE FROM eval_matches WHERE kind = 'anchor'")
    op.drop_column('eval_matches', 'anchor_index')
    op.drop_column('eval_matches', 'anchor_set_id')
    op.alter_column('eval_matches', 'b_member_id', existing_type=_uuid(), nullable=False)
    op.alter_column('eval_matches', 'a_member_id', existing_type=_uuid(), nullable=False)
    op.drop_index('ix_eval_anchor_runs_set', table_name='eval_anchor_runs')
    op.drop_table('eval_anchor_runs')
    op.drop_index('ix_eval_anchor_sets_ws_module', table_name='eval_anchor_sets')
    op.drop_table('eval_anchor_sets')
    op.drop_column('eval_tournaments', 'acknowledged_gates')
    op.drop_column('eval_tournaments', 'dropped_families')
    op.drop_column('eval_tournaments', 'diagnostics')
