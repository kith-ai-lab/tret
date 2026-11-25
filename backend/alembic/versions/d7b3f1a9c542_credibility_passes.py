"""credibility passes (Engine 2)

Revision ID: d7b3f1a9c542
Revises: c5d1a8f3e726
Create Date: 2026-11-25 14:00:00.000000

Engine 2 of the disclosure evaluation engine (PR 8): `credibility_passes` (one sealed Credibility
pass: its arms, frozen models, budget, spend, gates and runner lease), `credibility_reviews` (one
report's whole-report review, with the hashes of the exact prompt and report text) and
`credibility_review_runs` (the spend ledger: one row per run id, so a call is booked exactly once).
Additions only: nothing existing changes.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd7b3f1a9c542'
down_revision = 'c5d1a8f3e726'
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
        'credibility_passes',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('workspace_id', _uuid(), _fk('workspaces.id'), nullable=False),
        sa.Column('project_id', _uuid(), _fk('projects.id', 'SET NULL'), nullable=True),
        sa.Column('name', sa.Text(), nullable=False, server_default=''),
        sa.Column('status', sa.Text(), nullable=False, server_default='draft'),
        sa.Column('arms', _jsonb(), nullable=False, server_default='[]'),
        sa.Column('prompt_version', sa.Text(), nullable=False),
        sa.Column('selection', _jsonb(), nullable=False, server_default='{}'),
        sa.Column('estimates', _jsonb(), nullable=False, server_default='{}'),
        sa.Column('budget_usd', sa.Numeric(12, 6), nullable=True),
        sa.Column('spent_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('diagnostics', _jsonb(), nullable=False, server_default='{}'),
        sa.Column('acknowledged_gates', _jsonb(), nullable=False, server_default='[]'),
        sa.Column('pause_reason', sa.Text(), nullable=True),
        sa.Column('lease_owner', sa.Text(), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('started_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index('ix_credibility_passes_ws_created', 'credibility_passes', ['workspace_id', 'created_at'])
    op.create_table(
        'credibility_reviews',
        sa.Column('id', _uuid(), primary_key=True),
        sa.Column('pass_id', _uuid(), _fk('credibility_passes.id'), nullable=False),
        sa.Column('disclosure_report_id', _uuid(), _fk('disclosure_reports.id'), nullable=False),
        sa.Column('arm_key', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('credibility_score', sa.Integer(), nullable=True),
        sa.Column('pushbacks', _jsonb(), nullable=False, server_default='[]'),
        sa.Column('story_breaks', _jsonb(), nullable=False, server_default='[]'),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('freeze_id', _uuid(), _fk('routing_freezes.id', 'SET NULL'), nullable=True),
        sa.Column('input_mode', sa.Text(), nullable=True),
        sa.Column('input_budget', sa.Integer(), nullable=True),
        sa.Column('report_sha256', sa.Text(), nullable=True),
        sa.Column('prompt_sha256', sa.Text(), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('run_id', _uuid(), _fk('runs.id', 'SET NULL'), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('claimed_by', sa.Text(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint('pass_id', 'disclosure_report_id'),
    )
    op.create_index('ix_credibility_reviews_pass_status', 'credibility_reviews', ['pass_id', 'status'])
    op.create_index('ix_credibility_reviews_report', 'credibility_reviews', ['disclosure_report_id'])
    op.create_table(
        'credibility_review_runs',
        sa.Column('run_id', _uuid(), primary_key=True),
        sa.Column('pass_id', _uuid(), _fk('credibility_passes.id'), nullable=False),
        sa.Column('review_id', _uuid(), _fk('credibility_reviews.id', 'SET NULL'), nullable=True),
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('booked_at', sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index('ix_credibility_review_runs_pass', 'credibility_review_runs', ['pass_id'])


def downgrade() -> None:
    op.drop_index('ix_credibility_review_runs_pass', table_name='credibility_review_runs')
    op.drop_table('credibility_review_runs')
    op.drop_index('ix_credibility_reviews_report', table_name='credibility_reviews')
    op.drop_index('ix_credibility_reviews_pass_status', table_name='credibility_reviews')
    op.drop_table('credibility_reviews')
    op.drop_index('ix_credibility_passes_ws_created', table_name='credibility_passes')
    op.drop_table('credibility_passes')
