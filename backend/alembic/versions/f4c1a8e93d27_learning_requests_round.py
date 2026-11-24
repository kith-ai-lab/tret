"""learning requests round (R01-R28, 2026-11-20)

Revision ID: f4c1a8e93d27
Revises: e5b8c2d7f013
Create Date: 2026-11-20 12:00:00.000000

- `conversation_groups` + `conversations.group_id` (R21): member-made session
  groups with editable titles. Deleting a group ungroups its sessions.
- `conversations.archived_at` (R22): archive hides a session from the main
  list; its runs, emissions and audit trail are untouched.
- `runs.stop_reason` (R01, R04, R10): why the run's loop ended when that is
  worth telling apart from a plain status: "max_tokens" (last reply hit the
  output limit), "out_of_steps" (iteration cap), "degenerate_output"
  (padding / repeated-token run detected).
- `runs.guardrails` (R25): the guardrail settings in force for this run and the
  measured overhead of the checks (repair turns, their tokens).
- `analysis_runs` (R08): the cohort-only unvetted calculation lane — model-
  written code, its sha, input hashes, output rows and hash, kept apart from
  the vetted `method_runs`.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f4c1a8e93d27'
down_revision = 'e5b8c2d7f013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'conversation_groups',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('title', sa.Text(), nullable=False, server_default='New group'),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('ix_conversation_groups_project_id', 'conversation_groups', ['project_id'])

    op.add_column(
        'conversations',
        sa.Column(
            'group_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('conversation_groups.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )
    op.create_index('ix_conversations_group_id', 'conversations', ['group_id'])
    op.add_column('conversations', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))

    op.add_column('runs', sa.Column('stop_reason', sa.Text(), nullable=True))
    op.add_column('runs', sa.Column('guardrails', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        'analysis_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('runs.id'), nullable=True),
        sa.Column('purpose', sa.Text(), nullable=False, server_default=''),
        sa.Column('code', sa.Text(), nullable=False),
        sa.Column('code_sha', sa.Text(), nullable=False),
        sa.Column('inputs', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('output_hash', sa.Text(), nullable=True),
        sa.Column('row_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('duration_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.Text(), nullable=False, server_default='completed'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('ix_analysis_runs_run_id', 'analysis_runs', ['run_id'])


def downgrade() -> None:
    op.drop_index('ix_analysis_runs_run_id', table_name='analysis_runs')
    op.drop_table('analysis_runs')
    op.drop_column('runs', 'guardrails')
    op.drop_column('runs', 'stop_reason')
    op.drop_column('conversations', 'archived_at')
    op.drop_index('ix_conversations_group_id', table_name='conversations')
    op.drop_column('conversations', 'group_id')
    op.drop_index('ix_conversation_groups_project_id', table_name='conversation_groups')
    op.drop_table('conversation_groups')
