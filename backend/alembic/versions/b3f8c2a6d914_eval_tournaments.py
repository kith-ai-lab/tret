"""eval tournaments

Revision ID: b3f8c2a6d914
Revises: a9d4e7c1f352
Create Date: 2026-11-24 20:00:00.000000

Engine 1 of the disclosure evaluation engine: `peer_pool_snapshots` and `peer_pool_members` (the
peer pools as they stood), `eval_tournaments`, `eval_matches` (one juror judgement each) and
`module_ratings`. All empty until a tournament is created. The pool member is the immutable unit:
it copies the module's text location, hash, word count and claim density, matches and ratings point
at members, and the links back to the report rows are SET NULL, so re-extracting or deleting a
report never changes or removes tournament history.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b3f8c2a6d914'
down_revision = 'a9d4e7c1f352'
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
        'peer_pool_snapshots',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('workspace_id', _uuid(), _fk('workspaces.id'), nullable=False),
        sa.Column('project_id', _uuid(), _fk('projects.id', 'SET NULL'), nullable=True),
        sa.Column('taxonomy_version', sa.Text(), nullable=False),
        sa.Column('min_n', sa.Integer(), nullable=False, server_default='15'),
        sa.Column('rule', sa.Text(), nullable=False),
        sa.Column('corpus_filter', _jsonb(), nullable=False),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index('ix_peer_pool_snapshots_ws', 'peer_pool_snapshots', ['workspace_id'])
    op.create_table(
        'peer_pool_members',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('snapshot_id', _uuid(), _fk('peer_pool_snapshots.id'), nullable=False),
        sa.Column('report_extraction_id', _uuid(), _fk('report_extractions.id', 'SET NULL'), nullable=True),
        sa.Column('report_module_id', _uuid(), _fk('report_modules.id', 'SET NULL'), nullable=True),
        sa.Column('disclosure_report_id', _uuid(), _fk('disclosure_reports.id', 'SET NULL'), nullable=True),
        sa.Column('text_key', sa.Text(), nullable=True),
        sa.Column('text_sha256', sa.Text(), nullable=True),
        sa.Column('word_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('claim_density', sa.Float(), nullable=True),
        sa.Column('company', sa.Text(), nullable=False, server_default=''),
        sa.Column('mask_names', _jsonb(), nullable=False),
        sa.Column('mask_domains', _jsonb(), nullable=False),
        sa.Column('pool_key', sa.Text(), nullable=False),
        sa.Column('pool_level', sa.Text(), nullable=False),
        sa.Column('regime', sa.Text(), nullable=False, server_default='other'),
        sa.Column('nace_division', sa.Text(), nullable=True),
        sa.UniqueConstraint('snapshot_id', 'report_module_id'),
    )
    op.create_index('ix_peer_pool_members_snapshot_pool', 'peer_pool_members', ['snapshot_id', 'pool_key'])
    op.create_index('ix_peer_pool_members_report_module', 'peer_pool_members', ['report_module_id'])
    op.create_table(
        'eval_tournaments',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('workspace_id', _uuid(), _fk('workspaces.id'), nullable=False),
        sa.Column('module', sa.Text(), nullable=False),
        sa.Column('module_set', sa.Text(), nullable=False),
        sa.Column('snapshot_id', _uuid(), _fk('peer_pool_snapshots.id'), nullable=False),
        sa.Column('judge_prompt_version', sa.Text(), nullable=False),
        sa.Column('module_def', _jsonb(), nullable=False),
        sa.Column('freeze_id', _uuid(), _fk('routing_freezes.id', 'SET NULL'), nullable=True),
        sa.Column('model_policy', _jsonb(), nullable=False),
        sa.Column('rounds', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('current_round', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('budget_usd', sa.Numeric(12, 6), nullable=True),
        sa.Column('spent_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('status', sa.Text(), nullable=False, server_default='draft'),
        sa.Column('pause_reason', sa.Text(), nullable=True),
        sa.Column('swap_rate', sa.Float(), nullable=False, server_default='0.05'),
        sa.Column('double_rate', sa.Float(), nullable=False, server_default='0.1'),
        sa.Column('seed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimates', _jsonb(), nullable=False),
        sa.Column('summary', _jsonb(), nullable=False),
        sa.Column('lease_owner', sa.Text(), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('started_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index('ix_eval_tournaments_ws_created', 'eval_tournaments', ['workspace_id', 'created_at'])
    op.create_table(
        'eval_matches',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('tournament_id', _uuid(), _fk('eval_tournaments.id'), nullable=False),
        sa.Column('pool_key', sa.Text(), nullable=False),
        sa.Column('round', sa.Integer(), nullable=False),
        sa.Column('a_member_id', _uuid(), _fk('peer_pool_members.id'), nullable=False),
        sa.Column('b_member_id', _uuid(), _fk('peer_pool_members.id'), nullable=False),
        sa.Column('shown_a_first', sa.Boolean(), nullable=False),
        sa.Column('juror_index', sa.Integer(), nullable=False),
        sa.Column('juror_family', sa.Text(), nullable=True),
        sa.Column('juror_model', sa.Text(), nullable=True),
        sa.Column('run_id', _uuid(), _fk('runs.id', 'SET NULL'), nullable=True),
        sa.Column('verdict', sa.Text(), nullable=True),
        sa.Column('strength', sa.Text(), nullable=True),
        sa.Column('rationale', sa.Text(), nullable=True),
        sa.Column('kind', sa.Text(), nullable=False, server_default='primary'),
        sa.Column('swap_of', _uuid(), _fk('eval_matches.id', 'SET NULL'), nullable=True),
        sa.Column('double_of', _uuid(), _fk('eval_matches.id', 'SET NULL'), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('claimed_by', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('mask_count_a', sa.Integer(), nullable=True),
        sa.Column('mask_count_b', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column('judged_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('tournament_id', 'round', 'a_member_id', 'b_member_id', 'kind', 'juror_index'),
    )
    op.create_index('ix_eval_matches_tournament_status', 'eval_matches', ['tournament_id', 'status'])
    op.create_index('ix_eval_matches_a_member', 'eval_matches', ['a_member_id'])
    op.create_index('ix_eval_matches_b_member', 'eval_matches', ['b_member_id'])
    op.create_table(
        'eval_match_runs',
        sa.Column('run_id', _uuid(), primary_key=True),
        sa.Column('tournament_id', _uuid(), _fk('eval_tournaments.id'), nullable=False),
        sa.Column('match_id', _uuid(), _fk('eval_matches.id', 'SET NULL'), nullable=True),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('booked_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index('ix_eval_match_runs_tournament', 'eval_match_runs', ['tournament_id'])
    op.create_table(
        'module_ratings',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('tournament_id', _uuid(), _fk('eval_tournaments.id'), nullable=False),
        sa.Column('pool_key', sa.Text(), nullable=False),
        sa.Column('member_id', _uuid(), _fk('peer_pool_members.id'), nullable=False),
        sa.Column('rating', sa.Float(), nullable=False),
        sa.Column('se', sa.Float(), nullable=False),
        sa.Column('lo90', sa.Float(), nullable=False),
        sa.Column('hi90', sa.Float(), nullable=False),
        sa.Column('matches', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('wins', sa.Float(), nullable=False, server_default='0'),
        sa.Column('band_kind', sa.Text(), nullable=True),
        sa.Column('band', sa.Integer(), nullable=True),
        sa.Column('word_count', sa.Integer(), nullable=True),
        sa.Column('claim_density', sa.Float(), nullable=True),
        sa.UniqueConstraint('tournament_id', 'member_id'),
    )
    op.create_index('ix_module_ratings_tournament_pool', 'module_ratings', ['tournament_id', 'pool_key'])
    op.create_index('ix_module_ratings_member', 'module_ratings', ['member_id'])


def downgrade() -> None:
    op.drop_index('ix_eval_match_runs_tournament', table_name='eval_match_runs')
    op.drop_table('eval_match_runs')
    op.drop_index('ix_module_ratings_member', table_name='module_ratings')
    op.drop_index('ix_module_ratings_tournament_pool', table_name='module_ratings')
    op.drop_table('module_ratings')
    op.drop_index('ix_eval_matches_b_member', table_name='eval_matches')
    op.drop_index('ix_eval_matches_a_member', table_name='eval_matches')
    op.drop_index('ix_eval_matches_tournament_status', table_name='eval_matches')
    op.drop_table('eval_matches')
    op.drop_index('ix_eval_tournaments_ws_created', table_name='eval_tournaments')
    op.drop_table('eval_tournaments')
    op.drop_index('ix_peer_pool_members_report_module', table_name='peer_pool_members')
    op.drop_index('ix_peer_pool_members_snapshot_pool', table_name='peer_pool_members')
    op.drop_table('peer_pool_members')
    op.drop_index('ix_peer_pool_snapshots_ws', table_name='peer_pool_snapshots')
    op.drop_table('peer_pool_snapshots')
